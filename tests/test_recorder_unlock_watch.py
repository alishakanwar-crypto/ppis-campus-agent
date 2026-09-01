"""A locked recorder must free itself once it has been left alone.

DVR 2's admin lock expires by itself, but only while nothing presents the
account, so the agent waits out a silence and then tries exactly once —
nobody has to reboot the recorder or restart the campus PC.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main

DVR = {"ip": "192.168.0.12", "username": "admin", "password": "secret"}


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "<DeviceInfo/>"


class _Client:
    def __init__(self, status_code=200, calls=None):
        self._status_code = status_code
        self._calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, auth=None):
        self._calls.append(url)
        return _Response(self._status_code)


class RecorderUnlockWatchTests(unittest.TestCase):
    def setUp(self):
        main._isapi_cooldowns.clear()
        main._refused_credentials.clear()
        main._auth_refused_since_ist.clear()
        main._isapi_last_success.clear()
        main._channel_auth_cooldowns.clear()
        main._last_auth_attempt.clear()
        main._auth_unlock_next_probe.clear()
        main._auth_unlock_quiet.clear()
        main._rtsp_attempts_while_refused.clear()
        main._rtsp_credentials_worked.clear()
        self._config = main.config
        main.config = {"dvrs": [DVR]}

    def tearDown(self):
        main.config = self._config

    def _refuse(self):
        main._mark_isapi_auth_rejected(DVR)

    def test_refusal_schedules_a_retry_instead_of_waiting_for_a_human(self):
        self._refuse()

        self.assertIn(DVR["ip"], main._auth_unlock_next_probe)
        entry = main.dvr_snapshot_health()[0]
        self.assertFalse(entry["held_until_password_change"])
        self.assertGreater(entry["retry_in_seconds"], 0)

    def test_recorder_is_left_alone_until_the_quiet_window_passes(self):
        self._refuse()
        calls = []

        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kw: _Client(200, calls)
        ):
            asyncio.run(main._unlock_refused_recorders())

        self.assertEqual(calls, [])
        self.assertTrue(main._credentials_refused(DVR))

    def test_a_successful_probe_brings_the_recorder_back_by_itself(self):
        self._refuse()
        self._make_probe_due()
        calls = []

        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kw: _Client(200, calls)
        ):
            asyncio.run(main._unlock_refused_recorders())

        self.assertEqual(len(calls), 1)
        self.assertFalse(main._credentials_refused(DVR))
        self.assertEqual(main.dvr_snapshot_health(), [])

    def test_a_failed_probe_waits_longer_before_trying_again(self):
        self._refuse()
        self._make_probe_due()
        calls = []

        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kw: _Client(401, calls)
        ):
            asyncio.run(main._unlock_refused_recorders())

        self.assertEqual(len(calls), 1)
        self.assertTrue(main._credentials_refused(DVR))
        self.assertEqual(
            main._auth_unlock_quiet[DVR["ip"]],
            main._AUTH_UNLOCK_QUIET_SECONDS * 2,
        )

        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kw: _Client(401, calls)
        ):
            asyncio.run(main._unlock_refused_recorders())

        self.assertEqual(len(calls), 1, "second probe ran before its silence")

    def test_the_backoff_is_capped(self):
        self._refuse()
        main._auth_unlock_quiet[DVR["ip"]] = main._AUTH_UNLOCK_MAX_QUIET_SECONDS
        self._make_probe_due()

        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kw: _Client(401)
        ):
            asyncio.run(main._unlock_refused_recorders())

        self.assertEqual(
            main._auth_unlock_quiet[DVR["ip"]],
            main._AUTH_UNLOCK_MAX_QUIET_SECONDS,
        )

    def test_a_recent_login_attempt_restarts_the_silence(self):
        self._refuse()
        self._make_probe_due()
        # Something (an RTSP fallback, say) just presented the login again.
        main._note_auth_attempt(DVR["ip"])
        calls = []

        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kw: _Client(200, calls)
        ):
            asyncio.run(main._unlock_refused_recorders())

        self.assertEqual(calls, [])
        self.assertTrue(main._credentials_refused(DVR))
        self.assertGreater(
            main._auth_unlock_next_probe[DVR["ip"]], main.time.monotonic()
        )

    def test_capture_paths_count_as_touching_the_recorder(self):
        asyncio.run(self._rtsp_attempt())

        self.assertIn(DVR["ip"], main._last_auth_attempt)

    async def _rtsp_attempt(self):
        with mock.patch.object(
            main._rtsp_capture_executor, "submit"
        ) as submit:
            future = main.concurrent.futures.Future()
            future.set_result(None)
            submit.return_value = future
            await main._capture_snapshot_rtsp(DVR, 26, background=True)

    def _make_probe_due(self):
        quiet = main._auth_unlock_quiet.get(
            DVR["ip"], main._AUTH_UNLOCK_QUIET_SECONDS
        )
        main._auth_unlock_next_probe[DVR["ip"]] = main.time.monotonic() - 1
        main._last_auth_attempt[DVR["ip"]] = main.time.monotonic() - quiet - 1


if __name__ == "__main__":
    unittest.main()
