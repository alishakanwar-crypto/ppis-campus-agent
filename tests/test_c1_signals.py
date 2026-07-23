import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

import gate_counter

IST = gate_counter.IST


class StableEventIdTests(unittest.TestCase):
    def test_is_deterministic_for_same_inputs(self):
        a = gate_counter.stable_event_id("C1", "2026-07-23", 5, 0)
        b = gate_counter.stable_event_id("C1", "2026-07-23", 5, 0)
        self.assertEqual(a, b)

    def test_differs_for_different_crossings(self):
        base = gate_counter.stable_event_id("C1", "2026-07-23", 5, 0)
        self.assertNotEqual(base, gate_counter.stable_event_id("C1", "2026-07-23", 5, 1))
        self.assertNotEqual(base, gate_counter.stable_event_id("C1", "2026-07-23", 6, 0))
        self.assertNotEqual(base, gate_counter.stable_event_id("C1", "2026-07-24", 5, 0))

    def test_contains_no_pii(self):
        # Only non-PII inputs are used, so the id is a plain hex digest.
        event_id = gate_counter.stable_event_id("C1", "2026-07-23", 5, 0)
        self.assertRegex(event_id, r"^[0-9a-f]{16}$")


class C1SignalBuilderTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 23, 9, 30, 0, tzinfo=IST)

    def test_signal_is_anonymous_and_non_additive(self):
        signal = gate_counter._build_c1_signal(
            "ENTRY GATE-OUTSIDE (CP Plus)", "queue", {"occupancy": 7}, self.now,
        )
        self.assertTrue(signal["verification_only"])
        self.assertEqual(signal["type"], "queue")
        self.assertNotIn("person_crop", signal)
        self.assertNotIn("name", signal)
        self.assertNotIn("snapshot", json.dumps(signal))
        self.assertTrue(signal["timestamp"].endswith("IST"))

    def test_signal_uses_supplied_event_id(self):
        signal = gate_counter._build_c1_signal(
            "C1", "wrong_way", {}, self.now, event_id="abc123",
        )
        self.assertEqual(signal["event_id"], "abc123")


class DwellTrackerTests(unittest.TestCase):
    def test_fires_once_after_threshold(self):
        tracker = gate_counter.DwellTracker(threshold_seconds=10)
        self.assertEqual(tracker.update([1], 0.0), [])
        self.assertEqual(tracker.update([1], 5.0), [])
        fired = tracker.update([1], 11.0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], 1)
        # Does not fire again for the same track.
        self.assertEqual(tracker.update([1], 20.0), [])

    def test_resets_when_track_disappears(self):
        tracker = gate_counter.DwellTracker(threshold_seconds=10)
        tracker.update([1], 0.0)
        tracker.update([], 20.0)  # track gone
        # Same id reused later starts a fresh dwell window.
        self.assertEqual(tracker.update([1], 25.0), [])
        self.assertEqual(tracker.update([1], 36.0)[0][0], 1)


class CameraHealthMonitorTests(unittest.TestCase):
    @staticmethod
    def _noise(seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        return rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)

    def test_offline_after_consecutive_missing_frames(self):
        monitor = gate_counter.CameraHealthMonitor(offline_frames=3)
        self.assertIsNone(monitor.observe(None))
        self.assertIsNone(monitor.observe(None))
        self.assertEqual(monitor.observe(None), "offline")

    def test_frozen_when_frames_are_identical(self):
        monitor = gate_counter.CameraHealthMonitor(frozen_frames=3)
        frame = self._noise(1)
        monitor.observe(frame)
        monitor.observe(frame.copy())
        monitor.observe(frame.copy())
        self.assertEqual(monitor.observe(frame.copy()), "frozen")

    def test_blocked_when_frames_are_dark(self):
        monitor = gate_counter.CameraHealthMonitor(dark_luma=20, bad_frames=2)
        dark = np.zeros((64, 64, 3), dtype=np.uint8)
        results = [monitor.observe(dark) for _ in range(5)]
        self.assertIn("blocked", results)

    def test_blurred_when_variance_low(self):
        monitor = gate_counter.CameraHealthMonitor(
            blur_variance=1000, dark_luma=0, bad_frames=2, frozen_frames=100,
        )
        # A flat mid-gray frame has ~zero Laplacian variance but is not dark.
        flat = np.full((64, 64, 3), 128, dtype=np.uint8)
        results = [monitor.observe(flat) for _ in range(5)]
        self.assertIn("blurred", results)

    def test_moved_when_scene_shifts(self):
        monitor = gate_counter.CameraHealthMonitor(
            moved_diff=10, bad_frames=2, blur_variance=0, dark_luma=0,
            frozen_frames=100,
        )
        # Establish a baseline, then feed a very different scene.
        for _ in range(3):
            monitor.observe(self._noise(2))
        results = [monitor.observe(self._noise(999)) for _ in range(5)]
        self.assertIn("moved", results)

    def test_only_fires_once_per_state(self):
        monitor = gate_counter.CameraHealthMonitor(offline_frames=1)
        self.assertEqual(monitor.observe(None), "offline")
        self.assertIsNone(monitor.observe(None))  # no repeat


class ReplayDiscrepancyTests(unittest.TestCase):
    def setUp(self):
        with gate_counter._CPPLUS_LIVE_HOURLY_LOCK:
            gate_counter._cpplus_live_hourly_in.clear()

    def test_emits_delta_and_is_verification_only(self):
        hour_start = datetime(2026, 7, 23, 9, 0, 0, tzinfo=IST)
        hour_end = hour_start + timedelta(hours=1)
        # Live worker recorded 8 INs during the hour.
        for _ in range(8):
            gate_counter._record_live_hourly_in(hour_start + timedelta(minutes=5))

        sent = {}
        with patch.object(
            gate_counter, "send_c1_signal_events",
            side_effect=lambda events: sent.setdefault("events", events) or True,
        ):
            gate_counter._emit_replay_discrepancy(
                {"name": "C1"}, hour_start, hour_end, 10, "camera_native_counter",
            )
        signal = sent["events"][0]
        self.assertEqual(signal["type"], "replay_discrepancy")
        self.assertTrue(signal["verification_only"])
        self.assertEqual(signal["data"]["live"], 8)
        self.assertEqual(signal["data"]["verified"], 10)
        self.assertEqual(signal["data"]["delta"], 2)

    def test_no_signal_without_live_baseline(self):
        hour_start = datetime(2026, 7, 23, 9, 0, 0, tzinfo=IST)
        with patch.object(gate_counter, "send_c1_signal_events") as sender:
            gate_counter._emit_replay_discrepancy(
                {"name": "C1"}, hour_start, hour_start + timedelta(hours=1),
                5, "camera_native_counter",
            )
        sender.assert_not_called()


class C1SignalTransportTests(unittest.TestCase):
    def test_signals_post_with_agent_secret_header(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            text = "ok"

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, json, headers):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return FakeResponse()

        with patch.object(gate_counter, "CPPLUS_SIGNALS_ENABLED", True), \
                patch.object(gate_counter.httpx, "Client", return_value=FakeClient()), \
                patch.dict("os.environ", {"AGENT_SECRET": "s3cret"}, clear=False):
            ok = gate_counter.send_c1_signal_events(
                [gate_counter._build_c1_signal(
                    "C1", "queue", {"occupancy": 9}, datetime.now(IST),
                )]
            )
        self.assertTrue(ok)
        self.assertEqual(captured["url"], gate_counter.C1_SIGNAL_API)
        self.assertEqual(captured["headers"].get("X-Agent-Secret"), "s3cret")

    def test_disabled_signals_do_not_post(self):
        with patch.object(gate_counter, "CPPLUS_SIGNALS_ENABLED", False), \
                patch.object(gate_counter.httpx, "Client") as client:
            ok = gate_counter.send_c1_signal_events(
                [gate_counter._build_c1_signal(
                    "C1", "queue", {}, datetime.now(IST),
                )]
            )
        self.assertTrue(ok)
        client.assert_not_called()


class AnonymousCrossingPayloadTests(unittest.TestCase):
    def test_crossing_ledger_stays_anonymous(self):
        # Regression: the audit ledger must never persist images or names.
        event = {
            "event_id": gate_counter.stable_event_id("C1", "2026-07-23", 3, 0),
            "timestamp": "23-07-2026 09:30:00 IST",
            "camera": "ENTRY GATE-OUTSIDE (CP Plus)",
            "direction": "IN",
            "tracker_id": 3,
            "attire_color": "blue",
            "daily_in": 1,
            "daily_out": 0,
            "person_crop": "should-not-persist",
            "name": "should-not-persist",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = gate_counter._append_cpplus_crossing_audit([event], Path(tmp))
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("person_crop", payload)
        self.assertNotIn("name", payload)
        self.assertEqual(payload["event_id"], event["event_id"])


if __name__ == "__main__":
    unittest.main()
