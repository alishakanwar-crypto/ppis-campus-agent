"""A recorder whose only road is video must not be shut for the day.

DVR 2's 2015 firmware answers 401 on ISAPI, so every one of its classrooms is
served over RTSP. A few stream failures in one busy minute used up the small
"attempts while refused" allowance and every room on it then returned
"credentials refused" for the rest of the day.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main

DVR2 = {"ip": "192.168.0.12", "username": "admin", "password": "secret"}
OTHER = {"ip": "192.168.0.11", "username": "admin", "password": "secret"}


class RtspRoadStaysOpenTests(unittest.TestCase):
    def setUp(self):
        main._refused_credentials.clear()
        main._rtsp_credentials_worked.clear()
        main._rtsp_attempts_while_refused.clear()
        main._rtsp_attempt_while_refused_at.clear()
        main._auth_unlock_quiet.clear()

    def _refuse(self, dvr):
        main._refused_credentials[dvr["ip"]] = main._dvr_credential_key(dvr)

    def test_a_video_only_recorder_keeps_its_road_after_many_failures(self):
        self._refuse(DVR2)

        for _ in range(20):
            main._note_rtsp_attempt_while_refused(DVR2)

        self.assertTrue(main._rtsp_worth_trying(DVR2))

    def test_another_recorder_is_still_left_alone_after_its_allowance(self):
        self._refuse(OTHER)

        for _ in range(main._RTSP_ATTEMPTS_WHILE_REFUSED):
            main._note_rtsp_attempt_while_refused(OTHER)

        self.assertFalse(main._rtsp_worth_trying(OTHER))

    def test_that_recorder_is_tried_again_after_a_long_quiet(self):
        self._refuse(OTHER)
        main._note_rtsp_attempt_while_refused(OTHER)
        key = (OTHER["ip"], main._dvr_credential_key(OTHER))
        main._rtsp_attempt_while_refused_at[key] = main.time.monotonic() - (
            max(
                main._RTSP_REFUSED_ATTEMPT_RETRY_SECONDS,
                main._AUTH_UNLOCK_QUIET_SECONDS,
            )
            + 1
        )

        self.assertTrue(main._rtsp_worth_trying(OTHER))

        main._note_rtsp_attempt_while_refused(OTHER)
        self.assertEqual(main._rtsp_attempts_while_refused[key], 1)

    def test_the_retry_waits_out_a_lengthened_unlock_quiet(self):
        self._refuse(OTHER)
        main._auth_unlock_quiet[OTHER["ip"]] = (
            main._AUTH_UNLOCK_QUIET_SECONDS * 4
        )
        main._note_rtsp_attempt_while_refused(OTHER)
        key = (OTHER["ip"], main._dvr_credential_key(OTHER))
        main._rtsp_attempt_while_refused_at[key] = (
            main.time.monotonic() - main._AUTH_UNLOCK_QUIET_SECONDS * 2
        )

        self.assertFalse(main._rtsp_worth_trying(OTHER))

    def test_a_frame_forgets_the_attempts(self):
        self._refuse(DVR2)
        main._note_rtsp_attempt_while_refused(DVR2)

        main._note_rtsp_success(DVR2)

        key = (DVR2["ip"], main._dvr_credential_key(DVR2))
        self.assertNotIn(key, main._rtsp_attempts_while_refused)
        self.assertNotIn(key, main._rtsp_attempt_while_refused_at)


if __name__ == "__main__":
    unittest.main()
