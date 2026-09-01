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

    def test_success_is_judged_per_agent_not_by_a_process_count(self):
        """An agent may spawn helper processes, so a total of 5 python
        processes is not a failure — a missing agent is."""
        script = _read("restart_all.bat")
        self.assertNotIn("Expected 3 processes but found", script)
        self.assertIn("'main.py','trueface_poller','gate_counter'", script)
        self.assertIn("[WARNING] Not running:", script)

    def test_the_role_check_only_looks_at_python_processes(self):
        """An unfiltered process list contains the checking command itself,
        whose text names all three agents, so it could never report one
        missing."""
        script = _read("restart_all.bat")
        role_check = script[script.index("$c = (Get-CimInstance"):]
        role_check = role_check[: role_check.index("\n")]
        self.assertIn("Name='python.exe'", role_check)
        self.assertIn("Name='pythonw.exe'", role_check)
        self.assertIn("Name='py.exe'", role_check)

    def test_verification_names_each_process(self):
        script = _read("restart_all.bat")
        self.assertNotIn('tasklist /FI "IMAGENAME eq python.exe"\n', script)
        for label in ("Campus Agent", "TrueFace Poller", "Gate Counter"):
            self.assertIn(f"$agent = '{label}'", script)

    def test_the_start_time_is_read_as_a_date_not_a_dmtf_string(self):
        """Get-CimInstance already returns CreationDate as a DateTime, so the
        WMI DMTF converter threw on every process and printed only errors."""
        script = _read("restart_all.bat")
        self.assertNotIn("ManagementDateTimeConverter", script)
        self.assertIn("$_.CreationDate.ToString('HH:mm:ss')", script)

    def test_powershell_errors_cannot_become_the_missing_agent_list(self):
        script = _read("restart_all.bat")
        for name, label in (
            ("main.py", "Campus Agent"),
            ("trueface_poller", "TrueFace Poller"),
            ("gate_counter", "Gate Counter"),
        ):
            self.assertIn(
                f'if "%%a"=="{name}" set "MISSING=!MISSING! {label}"', script
            )
        self.assertIn("2^>nul'", script)

    def test_a_surviving_process_is_not_reported_as_success(self):
        script = _read("restart_all.bat")
        stale = script.index("still run the OLD code")
        success = script.index("All 3 agents started successfully")
        self.assertLess(stale, success)
        self.assertIn("$_.StartTime -lt (Get-Date).AddMinutes(-3)", script)


if __name__ == "__main__":
    unittest.main()
