import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import last_run


class PreviousRunSummaryTests(unittest.TestCase):
    def _summary(self, agent_text="", wrapper_text=""):
        with TemporaryDirectory() as folder:
            agent_log = Path(folder) / "campus_agent.log"
            wrapper_log = Path(folder) / "wrapper_campus.log"
            if agent_text:
                agent_log.write_text(agent_text, encoding="utf-8")
            if wrapper_text:
                wrapper_log.write_text(wrapper_text, encoding="utf-8")
            with (
                patch.object(last_run, "AGENT_LOG", agent_log),
                patch.object(last_run, "WRAPPER_LOG", wrapper_log),
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
                "2026-09-11 11:05:51,000 [INFO] ppis-agent: started\n"
                "2026-09-11 11:07:20,000 [ERROR] ppis-agent: camera gone\n"
                "2026-09-11 11:07:30,000 [CRITICAL] ppis-agent: out of memory\n"
            ),
            wrapper_text=(
                "[11/09/2026 11:07:31] Agent stopped (exit code: 3). "
                "Restarting in 10 seconds...\n"
            ),
        )
        self.assertEqual(summary["ended_at"], "2026-09-11 11:07:30")
        self.assertEqual(summary["exit_code"], "3")
        self.assertIn("out of memory", summary["last_error"])

    def test_campus_paths_and_secrets_are_not_carried_to_the_cloud(self):
        summary = self._summary(
            agent_text=(
                "2026-09-11 11:07:30,000 [ERROR] ppis-agent: failed reading "
                "C:\\PPIS\\ppis-campus-agent\\config.json secret=abc123\n"
            )
        )
        self.assertNotIn("C:\\PPIS", summary["last_error"])
        self.assertNotIn("abc123", summary["last_error"])
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
