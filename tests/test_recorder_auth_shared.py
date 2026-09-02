"""A lockout seen by one process must stop every other process too."""

import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder_auth


class SharedRecorderStateTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._state = Path(self._dir.name) / "recorder_auth.json"
        env = patch.dict(
            os.environ, {"RECORDER_AUTH_STATE": str(self._state)}
        )
        env.start()
        self.addCleanup(env.stop)
        importlib.reload(recorder_auth)
        self.addCleanup(importlib.reload, recorder_auth)

    def test_a_refusal_is_visible_to_every_process(self):
        recorder_auth.note_refusal("192.168.0.12", "keyA")
        self.assertTrue(recorder_auth.is_refused("192.168.0.12", "keyA"))
        # A second process, importing the module afresh, reads the same answer.
        second_process = importlib.reload(recorder_auth)
        self.assertTrue(second_process.is_refused("192.168.0.12", "keyA"))

    def test_a_new_password_lifts_the_refusal(self):
        recorder_auth.note_refusal("192.168.0.12", "keyA")
        self.assertFalse(recorder_auth.is_refused("192.168.0.12", "keyB"))

    def test_an_untouched_recorder_becomes_probe_due(self):
        recorder_auth.note_refusal("192.168.0.12", "keyA")
        self.assertFalse(recorder_auth.probe_due("192.168.0.12"))
        past = time.time() - (recorder_auth.QUIET_SECONDS + 60)
        recorder_auth._update(
            "192.168.0.12", next_probe_at=past, last_attempt=past
        )
        self.assertTrue(recorder_auth.probe_due("192.168.0.12"))

    def test_a_login_from_another_process_postpones_the_probe(self):
        recorder_auth.note_refusal("192.168.0.12", "keyA")
        past = time.time() - (recorder_auth.QUIET_SECONDS + 60)
        recorder_auth._update("192.168.0.12", next_probe_at=past)
        recorder_auth.note_attempt("192.168.0.12")
        self.assertFalse(recorder_auth.probe_due("192.168.0.12"))

    def test_a_failed_probe_doubles_the_silence_and_is_capped(self):
        recorder_auth.note_refusal("192.168.0.12", "keyA")
        first = recorder_auth.note_probe_failed("192.168.0.12")
        self.assertEqual(first, recorder_auth.QUIET_SECONDS * 2)
        for _ in range(20):
            last = recorder_auth.note_probe_failed("192.168.0.12")
        self.assertEqual(last, recorder_auth.MAX_QUIET_SECONDS)

    def test_a_success_clears_the_refusal_for_everyone(self):
        recorder_auth.note_refusal("192.168.0.12", "keyA")
        recorder_auth.note_success("192.168.0.12")
        self.assertFalse(recorder_auth.is_refused("192.168.0.12", "keyA"))
        self.assertTrue(recorder_auth.recently_worked("192.168.0.12"))

    def test_clearing_does_not_pretend_the_recorder_served_us(self):
        recorder_auth.note_refusal("192.168.0.12", "keyA")
        recorder_auth.clear("192.168.0.12")
        self.assertFalse(recorder_auth.is_refused("192.168.0.12", "keyA"))
        self.assertFalse(recorder_auth.recently_worked("192.168.0.12"))

    def test_a_corrupt_state_file_never_blocks_a_recorder(self):
        with open(recorder_auth.STATE_PATH, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertFalse(recorder_auth.is_refused("192.168.0.12", "keyA"))
        recorder_auth.note_refusal("192.168.0.12", "keyA")
        self.assertTrue(recorder_auth.is_refused("192.168.0.12", "keyA"))

    def test_the_same_login_hashes_the_same_everywhere(self):
        self.assertEqual(
            recorder_auth.credential_key("admin", "secret"),
            recorder_auth.credential_key("admin", "secret"),
        )
        self.assertNotEqual(
            recorder_auth.credential_key("admin", "secret"),
            recorder_auth.credential_key("admin", "other"),
        )


if __name__ == "__main__":
    unittest.main()
