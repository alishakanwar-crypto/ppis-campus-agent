"""DVR 2 came back for a few minutes and then refused again, every half hour.

The lockout was ours: once ISAPI refused, each parent photo of one of its
classrooms fell back to the video stream, and that stream presents the same
account. Every fallback re-armed the recorder's lock and pushed the quiet
unlock probe further away, so the recorder never got the silence it needs.
"""
import unittest
from unittest.mock import patch

import main


class Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.content = b""
        self.headers = {"content-type": "image/jpeg"}
        self.history = ()


class RecordingClient:
    async def get(self, url, auth=None):
        return Response(401)


LOCKED = {
    "ip": "192.0.2.92",
    "port": 80,
    "username": "admin",
    "password": "shared-password",
}


class NoLoginOnLockedDvrTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._live_capture_silent_channels.clear()
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()
        main._isapi_last_success.clear()
        main._rtsp_cooldowns.clear()
        main._refused_credentials.clear()
        main._auth_refused_since_ist.clear()
        main._rtsp_credentials_worked.clear()
        main._rtsp_attempts_while_refused.clear()
        main._channel_auth_cooldowns.clear()
        main._last_auth_attempt.clear()

    async def test_one_stream_only_after_a_refusal(self):
        """A second parent asking for a locked recorder's class costs no login."""
        streams = []

        async def stream(dvr, channel, background=False):
            streams.append((dvr["ip"], channel))

        with patch.object(
            main, "_get_live_dvr_client", return_value=RecordingClient()
        ), patch.object(main, "_capture_snapshot_rtsp", side_effect=stream):
            for _ in range(4):
                main._rtsp_cooldowns.clear()
                self.assertIsNone(await main.capture_snapshot(LOCKED, 7))

        self.assertEqual(len(streams), main._RTSP_ATTEMPTS_WHILE_REFUSED)
        self.assertFalse(main._rtsp_worth_trying(LOCKED))

    async def test_a_stream_that_logs_in_fine_leaves_the_probe_alone(self):
        """DVR 4 streams while its ISAPI answers 401, so its account is not
        locked and the one unlock probe must still get its turn."""
        main._rtsp_credentials_worked[LOCKED["ip"]] = main._dvr_credential_key(
            LOCKED
        )
        with patch.object(main, "_capture_frame_rtsp", return_value=b"frame"):
            self.assertEqual(
                await main._capture_snapshot_rtsp(LOCKED, 7), b"frame"
            )

        self.assertNotIn(LOCKED["ip"], main._last_auth_attempt)

    async def test_a_stream_on_a_refused_login_postpones_the_probe(self):
        with patch.object(main, "_capture_frame_rtsp", return_value=None):
            await main._capture_snapshot_rtsp(LOCKED, 7)

        self.assertIn(LOCKED["ip"], main._last_auth_attempt)


if __name__ == "__main__":
    unittest.main()
