import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8", errors="replace")


class RestartKillsOldProcessesTests(unittest.TestCase):
    def test_the_kill_runs_before_anything_is_checked(self):
        """tasklist ANDs several /FI IMAGENAME filters, so it never matched and
        the kill was skipped entirely — agents kept running pre-pull code."""
        script = _read("restart_all.bat")
        kill = script.index("taskkill /F /IM python.exe")
        check = script.index("call :count_python")
        self.assertLess(kill, check)
        self.assertNotIn(
            'tasklist /FI "IMAGENAME eq python.exe" /FI "IMAGENAME eq py.exe"',
            script,
        )

    def test_python_processes_are_counted_by_powershell(self):
        script = _read("restart_all.bat")
        self.assertIn(":count_python", script)
        self.assertIn("Get-Process python,py,pythonw", script)

    def test_a_surviving_process_is_not_reported_as_success(self):
        script = _read("restart_all.bat")
        stale = script.index("still run the OLD code")
        success = script.index("All 3 agents started successfully")
        self.assertLess(stale, success)
        self.assertIn("$_.StartTime -lt (Get-Date).AddMinutes(-3)", script)


if __name__ == "__main__":
    unittest.main()
