"""A restart must not strand a refused recorder without a recovery probe.

The refusal survives in the shared store, so a freshly started agent adopts it
— but it also has to adopt when the probe is due, or the recorder's classrooms
stay dark until somebody changes a password nobody changed.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
import recorder_auth

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


class RestartProbeTests(unittest.TestCase):
    def setUp(self):
        for store in (
            main._isapi_cooldowns, main._refused_credentials,
            main._auth_refused_since_ist, main._isapi_last_success,
            main._channel_auth_cooldowns, main._last_auth_attempt,
            main._auth_unlock_next_probe, main._auth_unlock_quiet,
        ):
            store.clear()
        self._config = main.config
        main.config = {"dvrs": [DVR]}

    def tearDown(self):
        main.config = self._config

    def _refused_before_restart(self, quiet: float) -> None:
        """A refusal left by an earlier run, its silence already served."""
        key = recorder_auth.credential_key("admin", "secret")
        now = recorder_auth.time.time()
        recorder_auth._update(
            DVR["ip"],
            refused_at=now - quiet - 10,
            credential_key=key,
            quiet_seconds=quiet,
            next_probe_at=now - 1,
            last_attempt=now - quiet - 1,
            last_success=None,
        )

    def test_a_restarted_agent_still_probes_a_recorder_it_never_refused(self):
        self._refused_before_restart(main._AUTH_UNLOCK_QUIET_SECONDS)
        calls = []

        self.assertTrue(main._credentials_refused(DVR))
        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kw: _Client(200, calls)
        ):
            asyncio.run(main._unlock_refused_recorders())

        self.assertEqual(len(calls), 1)
        self.assertFalse(main._credentials_refused(DVR))

    def test_the_doubled_silence_survives_the_restart(self):
        doubled = main._AUTH_UNLOCK_QUIET_SECONDS * 4
        self._refused_before_restart(doubled)

        main._credentials_refused(DVR)

        self.assertEqual(main._auth_unlock_quiet[DVR["ip"]], doubled)

    def test_an_adopted_refusal_is_not_probed_before_its_silence(self):
        key = recorder_auth.credential_key("admin", "secret")
        now = recorder_auth.time.time()
        recorder_auth._update(
            DVR["ip"],
            refused_at=now,
            credential_key=key,
            quiet_seconds=main._AUTH_UNLOCK_QUIET_SECONDS,
            next_probe_at=now + main._AUTH_UNLOCK_QUIET_SECONDS,
            last_attempt=now,
        )
        calls = []

        self.assertTrue(main._credentials_refused(DVR))
        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kw: _Client(200, calls)
        ):
            asyncio.run(main._unlock_refused_recorders())

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
