import json
import unittest
from unittest.mock import patch

from fake_camera import JPEG

import main


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


DVR = {
    "ip": "192.0.2.90",
    "port": 80,
    "username": "admin",
    "password": "secret",
}
CAMERAS = [(DVR, 5, "G1A  C1"), (DVR, 12, "G1A  C2")]


class CameraOutcomeLedgerTests(unittest.TestCase):
    def setUp(self):
        main._camera_outcomes.clear()

    def tearDown(self):
        main._camera_outcomes.clear()

    def test_a_camera_that_serves_is_not_reported(self):
        main._note_camera_outcome(CAMERAS[0], "GRADE 1A", True, {})
        self.assertEqual(main.camera_snapshot_health(), [])

    def test_a_camera_that_keeps_failing_is_named_with_its_reason(self):
        """Or nobody at school knows which camera to go and look at."""
        for _ in range(3):
            main._note_camera_outcome(
                CAMERAS[1],
                "GRADE 1A",
                False,
                {"outcome": "timed out", "exception": "TimeoutError"},
            )

        reported = main.camera_snapshot_health()

        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["camera"], "G1A  C2")
        self.assertEqual(reported[0]["classroom"], "GRADE 1A")
        self.assertEqual(reported[0]["channel"], 12)
        self.assertEqual(reported[0]["failures_in_a_row"], 3)
        self.assertIn("timed out", reported[0]["reason"])
        self.assertTrue(reported[0]["last_failed_ist"])

    def test_one_picture_clears_the_camera_of_its_failures(self):
        for _ in range(5):
            main._note_camera_outcome(CAMERAS[1], "GRADE 1A", False, {})
        main._note_camera_outcome(CAMERAS[1], "GRADE 1A", True, {})

        self.assertEqual(main.camera_snapshot_health(), [])
        self.assertFalse(main._camera_is_silent(CAMERAS[1]))

    def test_a_single_failure_is_not_called_silent(self):
        """A busy recorder fails one photo all the time and still serves."""
        main._note_camera_outcome(CAMERAS[1], "GRADE 1A", False, {})
        self.assertFalse(main._camera_is_silent(CAMERAS[1]))


class SilentCameraRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._camera_outcomes.clear()

    def tearDown(self):
        main._camera_outcomes.clear()

    async def test_an_out_of_order_camera_is_not_asked_twice(self):
        """Its second ask only spends a slot the working angle needs."""
        for _ in range(main._CAMERA_SILENT_AFTER_FAILURES):
            main._note_camera_outcome(CAMERAS[1], "GRADE 1A", False, {})
        attempts = {5: 0, 12: 0}

        async def capture(classroom, camera):
            _, channel, desc = camera
            attempts[channel] += 1
            if channel == 12:
                return None
            return JPEG, f"{desc}.jpg", desc, {}

        ws = FakeWs()
        with patch.object(
            main, "find_all_cameras_for_classroom", return_value=CAMERAS
        ), patch.object(
            main, "_capture_classroom_camera", capture
        ), patch.object(main, "compress_jpeg", lambda data, *a, **k: data):
            await main._handle_snapshot_request(ws, "GRADE 1A", "req-silent")

        self.assertEqual(attempts[12], 1)
        images = [m for m in ws.sent if m["type"] == "snapshot_image"]
        self.assertEqual([m["description"] for m in images], ["G1A  C1"])
        self.assertTrue(any(m["type"] == "snapshot_complete" for m in ws.sent))

    async def test_a_camera_whose_capture_raises_is_counted_too(self):
        """Otherwise an always-broken channel is never named to anyone."""
        async def capture(classroom, camera):
            _, channel, desc = camera
            if channel == 12:
                raise RuntimeError("stream exploded")
            return JPEG, f"{desc}.jpg", desc, {}

        ws = FakeWs()
        with patch.object(
            main, "find_all_cameras_for_classroom", return_value=CAMERAS
        ), patch.object(
            main, "_capture_classroom_camera", capture
        ), patch.object(main, "compress_jpeg", lambda data, *a, **k: data):
            await main._handle_snapshot_request(ws, "GRADE 1A", "req-raise")

        entry = main._camera_outcomes[("192.0.2.90", 12)]
        self.assertGreaterEqual(entry["failures_in_a_row"], 1)
        self.assertIn("stream exploded", entry["reason"])
        images = [m for m in ws.sent if m["type"] == "snapshot_image"]
        self.assertEqual([m["description"] for m in images], ["G1A  C1"])

    async def test_a_busy_camera_is_still_asked_twice(self):
        attempts = {5: 0, 12: 0}

        async def capture(classroom, camera):
            _, channel, desc = camera
            attempts[channel] += 1
            if channel == 12 and attempts[channel] == 1:
                return None
            return JPEG, f"{desc}.jpg", desc, {}

        ws = FakeWs()
        with patch.object(
            main, "find_all_cameras_for_classroom", return_value=CAMERAS
        ), patch.object(
            main, "_capture_classroom_camera", capture
        ), patch.object(main, "compress_jpeg", lambda data, *a, **k: data):
            await main._handle_snapshot_request(ws, "GRADE 1A", "req-busy")

        self.assertEqual(attempts[12], 2)
        images = [m for m in ws.sent if m["type"] == "snapshot_image"]
        self.assertEqual(len(images), 2)


if __name__ == "__main__":
    unittest.main()
