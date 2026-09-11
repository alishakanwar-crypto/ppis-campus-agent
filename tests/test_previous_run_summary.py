import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import last_run


class PreviousRunSummaryTests(unittest.TestCase):
    def _summary(self, agent_text="", wrapper_text="", wrapper_old_text=""):
        with TemporaryDirectory() as folder:
            agent_log = Path(folder) / "campus_agent.log"
            wrapper_log = Path(folder) / "wrapper_campus.log"
            wrapper_old = Path(folder) / "wrapper_campus.log.old"
            if agent_text:
                agent_log.write_text(agent_text, encoding="utf-8")
            if wrapper_text:
                wrapper_log.write_text(wrapper_text, encoding="utf-8")
            if wrapper_old_text:
                wrapper_old.write_text(wrapper_old_text, encoding="utf-8")
            with (
                patch.object(last_run, "AGENT_LOG", agent_log),
                patch.object(last_run, "WRAPPER_LOG", wrapper_log),
                patch.object(last_run, "WRAPPER_LOG_OLD", wrapper_old),
            ):
                return last_run.previous_run_summary()

    def test_missing_logs_are_harmless(self):
        self.assertEqual(
            last_run.previous_run_summary().keys(),
            {"ended_at", "exit_code", "last_error"},
        )
        summary = self._summary()
        self.assertEqual(summary["ended_at"], "")
        self.assertEqual(summary["exit_code"], "")
        self.assertEqual(summary["last_error"], "")

    def test_reports_when_the_last_run_stopped_and_what_it_said(self):
        summary = self._summary(
            agent_text=(
                "2026-09-11 11:05:51,000 [INFO] ppis-agent: AGENT RUN START\n"
                "2026-09-11 11:07:20,000 [ERROR] ppis-agent: camera gone\n"
                "2026-09-11 11:07:30,000 [CRITICAL] ppis-agent: out of memory\n"
            ),
            wrapper_text=(
                "[11/09/2026 11:07:31] Agent stopped (exit code: 3). "
                "Restarting in 10 seconds...\n"
            ),
        )
        self.assertEqual(summary["ended_at"], "11/09/2026 11:07:31")
        self.assertEqual(summary["exit_code"], "3")
        self.assertIn("out of memory", summary["last_error"])

    def test_a_silent_crash_is_timed_by_the_wrapper_not_the_last_log_line(
        self,
    ):
        summary = self._summary(
            agent_text=(
                "2026-09-11 10:00:00,000 [INFO] ppis-agent: AGENT RUN START\n"
                "2026-09-11 10:00:01,000 [INFO] ppis-agent: serving\n"
            ),
            wrapper_text=(
                "[11/09/2026 10:30:44] Agent stopped (exit code: -1). "
                "Restarting in 10 seconds...\n"
            ),
        )
        self.assertEqual(summary["ended_at"], "11/09/2026 10:30:44")
        self.assertEqual(summary["exit_code"], "-1")

    def test_a_capped_wrapper_log_still_gives_up_its_exit_code(self):
        summary = self._summary(
            wrapper_text="[11/09/2026 10:30:55] Starting revision abc1234\n",
            wrapper_old_text=(
                "[11/09/2026 10:30:44] Agent stopped (exit code: 3). "
                "Restarting in 10 seconds...\n"
            ),
        )
        self.assertEqual(summary["exit_code"], "3")
        self.assertEqual(summary["ended_at"], "11/09/2026 10:30:44")

    def test_an_error_from_a_run_before_the_last_one_is_not_reported(self):
        summary = self._summary(
            agent_text=(
                "2026-09-11 08:00:00,000 [INFO] ppis-agent: AGENT RUN START\n"
                "2026-09-11 08:00:10,000 [ERROR] ppis-agent: camera gone\n"
                "2026-09-11 09:00:00,000 [INFO] ppis-agent: AGENT RUN START\n"
                "2026-09-11 09:00:01,000 [INFO] ppis-agent: serving\n"
            )
        )
        self.assertEqual(summary["last_error"], "")

    def test_a_quoted_error_in_our_own_warning_is_not_a_new_failure(self):
        summary = self._summary(
            agent_text=(
                "2026-09-11 09:00:00,000 [INFO] ppis-agent: AGENT RUN START\n"
                "2026-09-11 09:00:01,000 [WARNING] ppis-agent: Previous run "
                "ended at 08:00 with exit code 3: 2026-09-11 08:00:10,000 "
                "[ERROR] ppis-agent: camera gone\n"
            )
        )
        self.assertEqual(summary["last_error"], "")

    def test_campus_paths_and_secrets_are_not_carried_to_the_cloud(self):
        summary = self._summary(
            agent_text=(
                "2026-09-11 11:07:30,000 [ERROR] ppis-agent: failed reading "
                "C:\\PPIS\\ppis-campus-agent\\config.json secret=abc123 "
                "key: 403623dc1024a4a3e7b0fa79ce21fe9c\n"
            )
        )
        self.assertNotIn("C:\\PPIS", summary["last_error"])
        self.assertNotIn("abc123", summary["last_error"])
        self.assertNotIn("403623dc1024a4a3e7b0fa79ce21fe9c",
                         summary["last_error"])
        self.assertIn("<path>", summary["last_error"])

    def test_a_clean_run_reports_nothing_to_worry_about(self):
        summary = self._summary(
            agent_text="2026-09-11 11:07:30,000 [INFO] ppis-agent: bye\n",
            wrapper_text="[11/09/2026 11:07:31] Pulling latest code...\n",
        )
        self.assertEqual(summary["last_error"], "")
        self.assertEqual(summary["exit_code"], "")
        self.assertEqual(summary["ended_at"], "2026-09-11 11:07:30")


if __name__ == "__main__":
    unittest.main()
