import unittest
from unittest.mock import patch

from fake_camera import JPEG

import main


class Response:
    def __init__(self, content: bytes):
        self.status_code = 200 if content else 404
        self.headers = {"content-type": "image/jpeg"} if content else {}
        self.content = content
        self.history = ()


DVR = {
    "ip": "192.0.2.96",
    "port": 80,
    "username": "admin",
    "password": "secret",
}


class ChannelRtspCooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._live_capture_best_pixels.clear()
        main._live_capture_slow_doors.clear()
        main._live_capture_silent_channels.clear()
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()
        main._rtsp_cooldowns.clear()
        main._rtsp_channel_cooldowns.clear()
        main._channel_auth_cooldowns.clear()
        main._isapi_last_success.clear()

    def test_one_camera_without_a_stream_does_not_rest_the_recorder(self):
        """Its classmates' rooms must keep the video road."""
        main._mark_rtsp_failure(DVR["ip"], 17)

        self.assertTrue(main._rtsp_channel_cooldown_active(DVR["ip"], 17))
        self.assertFalse(main._rtsp_cooldown_active(DVR["ip"]))
        self.assertFalse(main._rtsp_channel_cooldown_active(DVR["ip"], 13))

    def test_a_second_failing_channel_rests_the_whole_recorder(self):
        """Two channels failing is the recorder, not the cameras."""
        main._mark_rtsp_failure(DVR["ip"], 17)
        main._mark_rtsp_failure(DVR["ip"], 13)

        self.assertTrue(main._rtsp_cooldown_active(DVR["ip"]))

    async def test_a_channel_whose_stream_failed_keeps_its_snapshot_door(self):
        """Its doors are the only road left, so silence must not skip them."""
        picture = JPEG
        requested: list[str] = []

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                return Response(picture)

        main._live_capture_silent_channels[(DVR["ip"], 17)] = (
            main.time.monotonic()
        )
        main._mark_rtsp_failure(DVR["ip"], 17)

        with patch.object(main, "_get_live_dvr_client", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(DVR, 17), picture)

        self.assertTrue(requested)


if __name__ == "__main__":
    unittest.main()
