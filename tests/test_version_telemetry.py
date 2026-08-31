import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VersionTelemetryTests(unittest.TestCase):
    def test_hello_carries_the_commit_and_start_time(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        hello = source[source.index('"type": "agent_hello"'):]
        hello = hello[: hello.index("\n                }))")]
        self.assertIn('"code_commit": _running_commit()', hello)
        self.assertIn('"started_at_ist": _process_started_at_ist()', hello)

    def test_the_commit_is_the_one_the_code_was_pulled_from(self):
        import main

        expected = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        ).stdout.strip()
        self.assertEqual(main._running_commit(), expected)

    def test_start_time_is_reported_in_ist(self):
        import main

        self.assertRegex(
            main._process_started_at_ist(),
            r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2} IST$",
        )
        self.assertEqual(
            main._PROCESS_STARTED_AT.utcoffset().total_seconds(), 5.5 * 3600
        )


if __name__ == "__main__":
    unittest.main()
