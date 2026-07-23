import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from gate_intelligence import GateIntelligenceConfig, GateIntelligenceMonitor

IST = ZoneInfo("Asia/Kolkata")


class GateIntelligenceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 23, 9, 0, tzinfo=IST)
        self.config = GateIntelligenceConfig(
            congestion_people=2,
            congestion_seconds=5,
            loiter_seconds=10,
            vehicle_dwell_seconds=12,
            offline_seconds=5,
            frozen_seconds=3,
            blurred_seconds=30,
            blocked_seconds=30,
            view_changed_seconds=60,
            health_clear_seconds=2,
            expected_direction="IN",
        )
        self.monitor = GateIntelligenceMonitor("C1", self.config)

    def test_congestion_and_loitering_emit_once(self):
        self.assertEqual(
            self.monitor.observe_tracks(self.start, {1, 2}, set()),
            [],
        )
        events = self.monitor.observe_tracks(
            self.start + timedelta(seconds=6),
            {1, 2},
            set(),
        )
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "congestion_started",
            ],
        )
        events = self.monitor.observe_tracks(
            self.start + timedelta(seconds=11),
            {1, 2},
            set(),
        )
        self.assertEqual(
            [event["event_type"] for event in events],
            ["loitering", "loitering"],
        )
        self.assertEqual(
            self.monitor.observe_tracks(
                self.start + timedelta(seconds=15),
                {1, 2},
                set(),
            ),
            [],
        )

    def test_after_hours_wrong_way_and_reversal_are_non_additive(self):
        events = self.monitor.observe_crossing(
            self.start,
            7,
            "OUT",
            official_hours=False,
        )
        self.assertEqual(
            {event["event_type"] for event in events},
            {"after_hours_movement", "wrong_way"},
        )
        reversal = self.monitor.observe_crossing(
            self.start + timedelta(seconds=20),
            7,
            "IN",
            official_hours=False,
        )
        self.assertIn(
            "direction_reversal",
            {event["event_type"] for event in reversal},
        )
        for event in [*events, *reversal]:
            self.assertTrue(event["verification_only"])
            self.assertNotIn("name", event)
            self.assertNotIn("person_crop", event)

    def test_vehicle_dwell_is_tracked_separately(self):
        self.monitor.observe_tracks(self.start, set(), {9})
        events = self.monitor.observe_tracks(
            self.start + timedelta(seconds=13),
            set(),
            {9},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "vehicle_dwell")
        self.assertEqual(
            self.monitor.vehicle_dwell_seconds(
                9,
                self.start + timedelta(seconds=15),
            ),
            15,
        )

    def test_offline_and_recovery_emit_once(self):
        self.assertEqual(
            self.monitor.observe_capture_failure(self.start),
            [],
        )
        events = self.monitor.observe_capture_failure(
            self.start + timedelta(seconds=6),
        )
        self.assertEqual(events[0]["metadata"]["state"], "offline")
        self.assertEqual(
            self.monitor.observe_capture_failure(
                self.start + timedelta(seconds=8),
            ),
            [],
        )
        frame = np.random.default_rng(1).integers(
            0,
            255,
            size=(72, 128, 3),
            dtype=np.uint8,
        )
        recovered = self.monitor.observe_frame(
            self.start + timedelta(seconds=9),
            frame,
            active_people=0,
        )
        self.assertEqual(recovered[0]["metadata"]["state"], "online")

    def test_frozen_frame_transition_is_deduplicated(self):
        frame = np.random.default_rng(2).integers(
            0,
            255,
            size=(72, 128, 3),
            dtype=np.uint8,
        )
        self.monitor.observe_frame(self.start, frame, active_people=0)
        self.monitor.observe_frame(
            self.start + timedelta(seconds=1),
            frame,
            active_people=0,
        )
        events = self.monitor.observe_frame(
            self.start + timedelta(seconds=5),
            frame,
            active_people=0,
        )
        frozen = [
            event for event in events if event["metadata"].get("state") == "frozen"
        ]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(
            self.monitor.observe_frame(
                self.start + timedelta(seconds=6),
                frame,
                active_people=0,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
