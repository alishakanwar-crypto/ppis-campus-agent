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
        main._rtsp_channel_failed_at.clear()
        main._rtsp_last_success_at.clear()
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

    def test_two_broken_cameras_do_not_rest_a_streaming_recorder(self):
        """GRADE 2A's two dead cameras were taking DVR 2's road from every room."""
        main._note_rtsp_frame(DVR["ip"])

        main._mark_rtsp_failure(DVR["ip"], 4)
        main._mark_rtsp_failure(DVR["ip"], 6)

        self.assertTrue(main._rtsp_channel_cooldown_active(DVR["ip"], 4))
        self.assertTrue(main._rtsp_channel_cooldown_active(DVR["ip"], 6))
        self.assertFalse(main._rtsp_cooldown_active(DVR["ip"]))
        self.assertFalse(main._rtsp_channel_cooldown_active(DVR["ip"], 41))

    def test_a_recorder_that_stopped_streaming_is_still_rested(self):
        """When its last frame is older than the rest itself, it is the recorder."""
        main._note_rtsp_frame(DVR["ip"])
        main._rtsp_last_success_at[DVR["ip"]] = (
            main.time.monotonic() - main._RTSP_COOLDOWN_SECONDS - 1
        )

        main._mark_rtsp_failure(DVR["ip"], 4)
        main._mark_rtsp_failure(DVR["ip"], 6)

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

    async def test_a_stale_stream_failure_is_still_the_parents_last_road(self):
        """Doors just failed; a failure from minutes ago must not refuse them.

        GRADE 2B returned nothing in 3s of a 15s budget for exactly this.
        """

        class Client:
            async def get(_self, url, auth):
                return Response(b"")

        main._mark_rtsp_failure(DVR["ip"], 9)
        main._rtsp_channel_failed_at[(DVR["ip"], 9)] = (
            main.time.monotonic() - 60
        )
        token = main._live_request_deadline.set(main.time.monotonic() + 15)
        try:
            with patch.object(
                main, "_get_live_dvr_client", return_value=Client()
            ), patch.object(
                main, "_capture_snapshot_rtsp", return_value=JPEG
            ) as stream:
                self.assertEqual(await main.capture_snapshot(DVR, 9), JPEG)
        finally:
            main._live_request_deadline.reset(token)

        self.assertTrue(stream.called)

    async def test_a_stream_that_just_failed_is_not_asked_twice(self):
        """Within one photo it is the road with nothing left to give."""

        class Client:
            async def get(_self, url, auth):
                return Response(b"")

        token = main._live_request_deadline.set(main.time.monotonic() + 15)
        try:
            main._mark_rtsp_failure(DVR["ip"], 9)
            with patch.object(
                main, "_get_live_dvr_client", return_value=Client()
            ), patch.object(
                main, "_capture_snapshot_rtsp", return_value=JPEG
            ) as stream:
                self.assertIsNone(await main.capture_snapshot(DVR, 9))
        finally:
            main._live_request_deadline.reset(token)

        self.assertFalse(stream.called)

    async def test_a_stale_rest_is_stale_without_a_request_to_measure(self):
        """A capture outside a parent's request has no deadline to compare."""

        class Client:
            async def get(_self, url, auth):
                return Response(b"")

        main._mark_rtsp_failure(DVR["ip"], 9)
        main._rtsp_channel_failed_at[(DVR["ip"], 9)] = (
            main.time.monotonic() - 60
        )
        with patch.object(
            main, "_get_live_dvr_client", return_value=Client()
        ), patch.object(
            main, "_capture_snapshot_rtsp", return_value=JPEG
        ) as stream:
            self.assertEqual(await main.capture_snapshot(DVR, 9), JPEG)

        self.assertTrue(stream.called)


if __name__ == "__main__":
    unittest.main()
