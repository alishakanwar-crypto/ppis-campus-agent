"""DVR 2 kept re-locking itself: every retry — parent snapshot, classroom
sweep, RTSP fallback, dashboard DVR test — presented the password it had just
refused, so the recorder's lockout never expired. Nothing may touch it again
until its password changes."""
import unittest
from unittest.mock import patch

import main


class Response:
    def __init__(self, status_code, content=b"", content_type="image/jpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}
        self.history = ()


class RecordingClient:
    def __init__(self, responses):
        self._responses = responses
        self.requests = []

    async def get(self, url, auth=None):
        self.requests.append(url)
        return self._responses(url)


DVR = {
    "ip": "192.0.2.91",
    "port": 80,
    "username": "admin",
    "password": "old-password",
}


class LockedRecorderHoldTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()
        main._rtsp_cooldowns.clear()
        main._refused_credentials.clear()
        main._auth_refused_since_ist.clear()
        main._rtsp_credentials_worked.clear()
        main._rtsp_attempts_while_refused.clear()
        main._isapi_last_success.clear()
        main._channel_auth_cooldowns.clear()

    async def test_the_pause_does_not_expire_on_a_timer(self):
        main._mark_isapi_auth_rejected(DVR)
        with patch.object(main.time, "monotonic", return_value=1e9):
            self.assertEqual(
                main._isapi_cooldown("192.0.2.91"), "credentials refused"
            )

    async def test_nothing_is_tried_once_rtsp_cannot_log_in_either(self):
        client = RecordingClient(lambda url: Response(401))
        with patch.object(main, "_get_live_dvr_client", return_value=client), \
                patch.object(main, "_capture_snapshot_rtsp", return_value=None):
            for _ in range(main._RTSP_ATTEMPTS_WHILE_REFUSED + 2):
                main._rtsp_cooldowns.clear()
                self.assertIsNone(await main.capture_snapshot(DVR, 5))
            rtsp_attempts = main._rtsp_attempts_while_refused[
                ("192.0.2.91", main._dvr_credential_key(DVR))
            ]

        # The recorder is left alone entirely, so its lockout can expire.
        self.assertEqual(rtsp_attempts, main._RTSP_ATTEMPTS_WHILE_REFUSED)
        self.assertFalse(main._rtsp_worth_trying(DVR))

    async def test_a_recorder_that_streams_over_rtsp_keeps_working(self):
        """DVR 4 answers 401 on ISAPI but streams fine, so a refusal alone must
        not silence it."""
        client = RecordingClient(lambda url: Response(401))
        with patch.object(main, "_get_live_dvr_client", return_value=client), \
                patch.object(
                    main, "_capture_snapshot_rtsp", return_value=b"rtsp-frame"
                ):
            for _ in range(main._RTSP_ATTEMPTS_WHILE_REFUSED + 3):
                self.assertEqual(
                    await main.capture_snapshot(DVR, 5), b"rtsp-frame"
                )

        self.assertTrue(main._rtsp_worth_trying(DVR))

    async def test_a_new_password_resumes_the_recorder(self):
        main._mark_isapi_auth_rejected(DVR)
        self.assertTrue(main._credentials_refused(DVR))

        renamed = dict(DVR, password="new-password")
        self.assertFalse(main._credentials_refused(renamed))

        client = RecordingClient(lambda url: Response(200, b"jpeg"))
        with patch.object(main, "_get_live_dvr_client", return_value=client):
            self.assertEqual(await main.capture_snapshot(renamed, 5), b"jpeg")

    async def test_the_dashboard_test_does_not_retry_a_refused_login(self):
        main._mark_isapi_auth_rejected(DVR)
        with patch.object(main.httpx, "AsyncClient") as client:
            result = await main.test_dvr_connection(DVR)

        client.assert_not_called()
        self.assertEqual(result["status"], "auth_failed")
        self.assertTrue(result["held_until_password_change"])

    async def test_the_dashboard_test_records_a_refusal_it_discovers(self):
        client = RecordingClient(lambda url: Response(401))

        class _Ctx:
            async def __aenter__(self_inner):
                return client

            async def __aexit__(self_inner, *exc):
                return False

        with patch.object(main.httpx, "AsyncClient", return_value=_Ctx()):
            result = await main.test_dvr_connection(DVR)

        self.assertEqual(result["status"], "auth_failed")
        self.assertTrue(main._credentials_refused(DVR))

    async def test_channel_discovery_leaves_a_refused_recorder_alone(self):
        main._mark_isapi_auth_rejected(DVR)
        with patch.dict(main.config, {"dvrs": [DVR]}, clear=False), \
                patch.object(main.httpx, "AsyncClient") as client:
            await main.discover_dvr_channel_names()

        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
