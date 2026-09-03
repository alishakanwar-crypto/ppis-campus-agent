import json
import time
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
    "ip": "192.0.2.81",
    "port": 80,
    "username": "admin",
    "password": "secret",
}
CAMERAS = [(DVR, 22, "NUR-3 C1"), (DVR, 26, "NUR-3 C2")]


class SecondAngleRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_second_camera_is_tried_again(self):
        """A parent must not get one photo of a two-camera classroom."""
        attempts = {22: 0, 26: 0}

        async def capture(classroom, camera):
            _, channel, desc = camera
            attempts[channel] += 1
            if channel == 26 and attempts[channel] == 1:
                return None
            return JPEG, f"{desc}.jpg", desc, {}

        ws = FakeWs()
        with patch.object(
            main, "find_all_cameras_for_classroom", return_value=CAMERAS
        ), patch.object(main, "_capture_classroom_camera", capture), patch.object(
            main, "compress_jpeg", lambda data, *a, **k: data
        ):
            await main._handle_snapshot_request(ws, "NUR-3", "req-1")

        images = [m for m in ws.sent if m["type"] == "snapshot_image"]
        self.assertEqual(
            sorted(m["description"] for m in images), ["NUR-3 C1", "NUR-3 C2"]
        )
        self.assertEqual(attempts[26], 2)
        self.assertEqual(attempts[22], 1)

    async def test_a_silent_classroom_is_tried_again_before_failing(self):
        """A busy recorder usually serves the second attempt."""
        attempts = {22: 0, 26: 0}

        async def capture(classroom, camera):
            _, channel, desc = camera
            attempts[channel] += 1
            if attempts[channel] == 1:
                return None
            return JPEG, f"{desc}.jpg", desc, {}

        ws = FakeWs()
        with patch.object(
            main, "find_all_cameras_for_classroom", return_value=CAMERAS
        ), patch.object(main, "_capture_classroom_camera", capture), patch.object(
            main, "compress_jpeg", lambda data, *a, **k: data
        ):
            await main._handle_snapshot_request(ws, "NUR-3", "req-silent")

        images = [m for m in ws.sent if m["type"] == "snapshot_image"]
        self.assertEqual(
            sorted(m["description"] for m in images), ["NUR-3 C1", "NUR-3 C2"]
        )
        self.assertFalse(
            [m for m in ws.sent if m["type"] == "snapshot_response"], ws.sent
        )

    async def test_no_retry_when_the_request_has_no_time_left(self):
        """Retrying must never cost the parent the photo already taken."""
        attempts = {22: 0, 26: 0}

        async def capture(classroom, camera):
            _, channel, desc = camera
            attempts[channel] += 1
            if channel == 26:
                return None
            return JPEG, f"{desc}.jpg", desc, {}

        ws = FakeWs()
        with patch.object(
            main, "find_all_cameras_for_classroom", return_value=CAMERAS
        ), patch.object(main, "_capture_classroom_camera", capture), patch.object(
            main, "compress_jpeg", lambda data, *a, **k: data
        ), patch.object(main, "_SNAPSHOT_LIVE_REQUEST_BUDGET_SECONDS", 1.0):
            await main._handle_snapshot_request(ws, "NUR-3", "req-2")

        self.assertEqual(attempts[26], 1)
        images = [m for m in ws.sent if m["type"] == "snapshot_image"]
        self.assertEqual(len(images), 1)

    async def test_a_camera_that_raises_does_not_lose_the_other_photo(self):
        async def capture(classroom, camera):
            _, channel, desc = camera
            if channel == 26:
                raise RuntimeError("stream exploded")
            return JPEG, f"{desc}.jpg", desc, {}

        ws = FakeWs()
        with patch.object(
            main, "find_all_cameras_for_classroom", return_value=CAMERAS
        ), patch.object(main, "_capture_classroom_camera", capture), patch.object(
            main, "compress_jpeg", lambda data, *a, **k: data
        ):
            await main._handle_snapshot_request(ws, "NUR-3", "req-3")

        images = [m for m in ws.sent if m["type"] == "snapshot_image"]
        self.assertEqual([m["description"] for m in images], ["NUR-3 C1"])
        self.assertTrue(any(m["type"] == "snapshot_complete" for m in ws.sent))


class BusyRecorderCooldownTests(unittest.TestCase):
    def setUp(self):
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()
        main._isapi_last_success.clear()

    def test_a_recorder_still_serving_is_not_sent_to_rtsp(self):
        """One deaf channel must not push a whole recorder onto video."""
        ip = "192.0.2.82"
        main._isapi_last_success[ip] = time.monotonic()
        for _ in range(main._ISAPI_TIMEOUTS_BEFORE_BACKOFF + 1):
            main._mark_isapi_timeout(ip)
        self.assertEqual(main._isapi_cooldown(ip), "")

    def test_a_silent_recorder_still_goes_to_rtsp(self):
        ip = "192.0.2.83"
        for _ in range(main._ISAPI_TIMEOUTS_BEFORE_BACKOFF):
            main._mark_isapi_timeout(ip)
        self.assertEqual(main._isapi_cooldown(ip), "not answering")


if __name__ == "__main__":
    unittest.main()
