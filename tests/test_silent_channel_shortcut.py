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
    "ip": "192.0.2.95",
    "port": 80,
    "username": "admin",
    "password": "secret",
}


class SilentChannelShortcutTests(unittest.IsolatedAsyncioTestCase):
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
        main._channel_auth_cooldowns.clear()
        main._isapi_last_success.clear()

    async def test_a_channel_whose_doors_never_answer_goes_straight_to_rtsp(self):
        """Knocking on silent doors again only delays the parent's photo."""
        requested: list[str] = []

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                raise main.httpx.ReadTimeout("no answer")

        with patch.object(main, "_get_live_dvr_client", return_value=Client()), \
                patch.object(
                    main, "_capture_snapshot_rtsp", return_value=b"rtsp-frame"
                ):
            self.assertEqual(await main.capture_snapshot(DVR, 11), b"rtsp-frame")
            doors_knocked = len(requested)
            self.assertEqual(await main.capture_snapshot(DVR, 11), b"rtsp-frame")

        self.assertEqual(len(requested), doors_knocked)
        self.assertTrue(main._channel_doors_silent(DVR["ip"], 11))

    async def test_a_channel_that_answers_is_not_shortcut(self):
        """One silent door must not send a working channel to the video stream."""
        picture = JPEG

        class Client:
            async def get(_self, url, auth):
                if "videoResolutionWidth" in url:
                    raise main.httpx.ReadTimeout("no answer")
                return Response(picture)

        with patch.object(main, "_get_live_dvr_client", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(DVR, 12), picture)

        self.assertFalse(main._channel_doors_silent(DVR["ip"], 12))

    async def test_the_shortcut_expires_so_a_repaired_camera_is_tried_again(self):
        main._live_capture_silent_channels[(DVR["ip"], 13)] = (
            main.time.monotonic()
            - main._LIVE_CAPTURE_SILENT_CHANNEL_TTL_SECONDS
            - 1
        )
        self.assertFalse(main._channel_doors_silent(DVR["ip"], 13))
        self.assertNotIn(
            (DVR["ip"], 13), main._live_capture_silent_channels
        )

    async def test_a_working_capture_forgets_the_shortcut(self):
        picture = JPEG
        main._live_capture_silent_channels[(DVR["ip"], 14)] = (
            main.time.monotonic()
        )

        class Client:
            async def get(_self, url, auth):
                return Response(picture)

        with patch.object(main, "_get_live_dvr_client", return_value=Client()), \
                patch.object(main, "_rtsp_cooldown_active", return_value=True):
            # RTSP is unusable, so the doors are still worth one knock.
            self.assertEqual(await main.capture_snapshot(DVR, 14), picture)

        self.assertNotIn(
            (DVR["ip"], 14), main._live_capture_silent_channels
        )


if __name__ == "__main__":
    unittest.main()
