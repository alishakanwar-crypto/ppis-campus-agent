"""A log file the agent holds open must not stop it taking merged code."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class _Run:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


LOCKED = (
    "error: unable to unlink old 'campus_agent.log': Invalid argument\n"
    "error: unable to unlink old 'gate_counter.log': Invalid argument\n"
)


class LockedLogPullTests(unittest.TestCase):
    def test_the_reset_is_retried_after_freeing_the_open_files(self):
        calls: list[tuple[str, ...]] = []

        def run(args, **kwargs):
            calls.append(tuple(args[1:]))
            if args[1] == "reset" and ("update-index",) not in [
                (call[0],) for call in calls[:-1]
            ]:
                return _Run(1, LOCKED)
            return _Run()

        with patch.object(main.subprocess, "run", side_effect=run), \
                patch.object(main, "_git", side_effect=["old1234", "new5678"]):
            self.assertEqual(main._pull_merged_code("new5678"), "")

        released = [call for call in calls if call[0] == "update-index"]
        self.assertEqual(len(released), 1)
        self.assertIn("campus_agent.log", released[0])
        self.assertIn("gate_counter.log", released[0])
        self.assertEqual(
            len([call for call in calls if call[0] == "reset"]), 2
        )

    def test_a_reset_that_keeps_failing_is_still_reported(self):
        def run(args, **kwargs):
            if args[1] == "reset":
                return _Run(1, LOCKED)
            return _Run()

        with patch.object(main.subprocess, "run", side_effect=run), \
                patch.object(main, "_git", side_effect=["old1234", "old1234"]):
            said = main._pull_merged_code("new5678")

        self.assertIn("unable to unlink", said)

    def test_a_failure_with_no_locked_file_is_not_retried(self):
        calls: list[tuple[str, ...]] = []

        def run(args, **kwargs):
            calls.append(tuple(args[1:]))
            if args[1] == "reset":
                return _Run(1, "fatal: ambiguous argument")
            return _Run()

        with patch.object(main.subprocess, "run", side_effect=run), \
                patch.object(main, "_git", side_effect=["old1234", "old1234"]):
            said = main._pull_merged_code("new5678")

        self.assertIn("ambiguous", said)
        self.assertEqual(
            len([call for call in calls if call[0] == "reset"]), 1
        )


if __name__ == "__main__":
    unittest.main()
