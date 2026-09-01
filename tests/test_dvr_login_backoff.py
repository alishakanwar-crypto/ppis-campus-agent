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

    async def get(self, url, auth):
        self.requests.append(url)
        return self._responses(url)


class TimingOutClient:
    def __init__(self):
        self.requests = 0

    async def get(self, url, auth):
        self.requests += 1
        raise main.httpx.ReadTimeout("no answer")


DVR = {
    "ip": "192.0.2.90",
    "port": 80,
    "username": "admin",
    "password": "secret",
}


class DvrLoginBackoffTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_refused_login_gives_up_at_once_and_falls_back_to_rtsp(self):
        client = RecordingClient(lambda url: Response(401))
        with patch.object(main, "_get_live_dvr_client", return_value=client), \
                patch.object(
                    main, "_capture_snapshot_rtsp", return_value=b"rtsp-frame"
                ):
            snapshot = await main.capture_snapshot(DVR, 5)

        self.assertEqual(snapshot, b"rtsp-frame")
        # One pass over the URL candidates, not three retried passes.
        self.assertLessEqual(len(client.requests), 6)
        self.assertEqual(
            main._isapi_cooldown("192.0.2.90"), "credentials refused"
        )

    async def test_one_stream_refusing_still_falls_back_to_another_stream(self):
        def reply(url):
            if "/502/picture" in url:
                return Response(200, b"jpeg")
            return Response(401)

        client = RecordingClient(reply)
        with patch.object(main, "_get_live_dvr_client", return_value=client):
            snapshot = await main.capture_snapshot(DVR, 5)

        self.assertEqual(snapshot, b"jpeg")
        self.assertEqual(main._isapi_cooldown("192.0.2.90"), "")

    async def test_a_missing_channel_does_not_hide_a_locked_recorder(self):
        """DVR 2's lockout looked like a plain capture failure because one of
        its doors answers 404, so the recorder never went on fallback."""
        def reply(url):
            if "/502/picture" in url:
                return Response(404, content_type="text/html")
            return Response(401)

        client = RecordingClient(reply)
        with patch.object(main, "_get_live_dvr_client", return_value=client), \
                patch.object(
                    main, "_capture_snapshot_rtsp", return_value=b"rtsp-frame"
                ):
            snapshot = await main.capture_snapshot(DVR, 5)

        self.assertEqual(snapshot, b"rtsp-frame")
        self.assertEqual(
            main._isapi_cooldown("192.0.2.90"), "credentials refused"
        )

    async def test_a_recorder_on_cooldown_is_not_asked_again(self):
        client = RecordingClient(lambda url: Response(401))
        with patch.object(main, "_get_live_dvr_client", return_value=client), \
                patch.object(
                    main, "_capture_snapshot_rtsp", return_value=b"rtsp-frame"
                ):
            await main.capture_snapshot(DVR, 5)
            requests_after_first = len(client.requests)
            snapshot = await main.capture_snapshot(DVR, 6)

        self.assertEqual(snapshot, b"rtsp-frame")
        self.assertEqual(len(client.requests), requests_after_first)

    async def test_a_working_capture_clears_the_cooldown(self):
        main._isapi_cooldowns["192.0.2.90"] = (float("inf"), "not answering")
        client = RecordingClient(lambda url: Response(200, b"jpeg"))
        with patch.object(main, "_get_live_dvr_client", return_value=client):
            main._isapi_cooldowns.clear()
            snapshot = await main.capture_snapshot(DVR, 7)

        self.assertEqual(snapshot, b"jpeg")
        self.assertEqual(main._isapi_cooldown("192.0.2.90"), "")

    async def test_repeated_timeouts_send_later_captures_straight_to_rtsp(self):
        client = TimingOutClient()
        with patch.object(main, "_get_live_dvr_client", return_value=client), \
                patch.object(main, "_capture_snapshot_rtsp", return_value=None):
            for _ in range(main._ISAPI_TIMEOUTS_BEFORE_BACKOFF):
                await main.capture_snapshot(DVR, 8)
            requests_before = client.requests
            main._rtsp_cooldowns.clear()
            with patch.object(
                main, "_capture_snapshot_rtsp", return_value=b"rtsp-frame"
            ):
                snapshot = await main.capture_snapshot(DVR, 8)

        self.assertEqual(snapshot, b"rtsp-frame")
        self.assertEqual(client.requests, requests_before)
        self.assertEqual(main._isapi_cooldown("192.0.2.90"), "not answering")

    async def test_one_slow_request_does_not_bypass_a_healthy_recorder(self):
        client = TimingOutClient()
        with patch.object(main, "_get_live_dvr_client", return_value=client), \
                patch.object(main, "_capture_snapshot_rtsp", return_value=None):
            await main.capture_snapshot(DVR, 8)

        self.assertEqual(main._isapi_cooldown("192.0.2.90"), "")

    async def test_a_silent_recorder_leaves_time_for_the_rtsp_fallback(self):
        attempted = []

        async def slow_get(url, auth):
            attempted.append(url)
            raise main.httpx.ReadTimeout("no answer")

        client = TimingOutClient()
        client.get = slow_get
        with patch.object(main, "_get_live_dvr_client", return_value=client), \
                patch.object(
                    main, "_capture_snapshot_rtsp", return_value=b"rtsp-frame"
                ):
            snapshot = await main.capture_snapshot(DVR, 9)

        self.assertEqual(snapshot, b"rtsp-frame")
        self.assertTrue(attempted)

    async def test_health_lists_the_recorders_being_bypassed(self):
        main._mark_isapi_auth_rejected(DVR)
        health = main.dvr_snapshot_health()

        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["ip"], "192.0.2.90")
        self.assertEqual(health[0]["reason"], "credentials refused")
        # Held until the unlock watch retries it, not on a capture timer.
        self.assertIsNone(health[0]["seconds_remaining"])
        self.assertFalse(health[0]["held_until_password_change"])
        self.assertGreater(health[0]["retry_in_seconds"], 0)

    async def test_background_capture_uses_rtsp_while_isapi_is_paused(self):
        main._mark_isapi_auth_rejected(DVR)
        with patch.object(
            main, "_capture_snapshot_rtsp", return_value=b"rtsp-frame"
        ):
            snapshot = await main.capture_snapshot(DVR, 10, background=True)

        self.assertEqual(snapshot, b"rtsp-frame")


if __name__ == "__main__":
    unittest.main()
