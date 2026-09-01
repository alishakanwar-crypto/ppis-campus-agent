"""One unusable channel must not take a whole recorder's classrooms dark.

DVR 4 answered 401 on five unused channels while it was serving 21 rooms, and
the agent paused the entire recorder until its password changed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main

DVR = {"ip": "192.168.0.13", "username": "admin", "password": "secret"}


class ChannelAuthRefusalTests(unittest.TestCase):
    def setUp(self):
        main._isapi_cooldowns.clear()
        main._refused_credentials.clear()
        main._auth_refused_since_ist.clear()
        main._isapi_last_success.clear()
        main._channel_auth_cooldowns.clear()

    def test_refusal_on_a_serving_recorder_rests_only_that_channel(self):
        main._isapi_last_success[DVR["ip"]] = main.time.monotonic()

        main._mark_isapi_auth_rejected(DVR, 36)

        self.assertFalse(main._credentials_refused(DVR))
        self.assertEqual(main.dvr_snapshot_health(), [])
        self.assertTrue(main._channel_auth_refused(DVR["ip"], 36))
        self.assertFalse(main._channel_auth_refused(DVR["ip"], 26))

    def test_recorder_is_still_held_when_nothing_has_worked(self):
        main._mark_isapi_auth_rejected(DVR, 36)

        self.assertTrue(main._credentials_refused(DVR))
        self.assertEqual(
            [entry["ip"] for entry in main.dvr_snapshot_health()], [DVR["ip"]]
        )

    def test_a_stale_success_no_longer_excuses_a_refusal(self):
        main._isapi_last_success[DVR["ip"]] = (
            main.time.monotonic() - main._AUTH_REFUSAL_TRUST_SECONDS - 1
        )

        main._mark_isapi_auth_rejected(DVR, 36)

        self.assertTrue(main._credentials_refused(DVR))

    def test_device_level_refusal_still_holds_the_recorder(self):
        main._isapi_last_success[DVR["ip"]] = main.time.monotonic()

        main._mark_isapi_auth_rejected(DVR)

        self.assertTrue(main._credentials_refused(DVR))

    def test_channel_rest_expires(self):
        main._channel_auth_cooldowns[(DVR["ip"], 36)] = (
            main.time.monotonic() - 1
        )

        self.assertFalse(main._channel_auth_refused(DVR["ip"], 36))
        self.assertNotIn((DVR["ip"], 36), main._channel_auth_cooldowns)


if __name__ == "__main__":
    unittest.main()
