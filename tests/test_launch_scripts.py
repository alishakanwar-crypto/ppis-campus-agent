import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8", errors="replace")


class LaunchScriptTests(unittest.TestCase):
    def test_restart_kills_python_launcher(self):
        script = _read("restart_all.bat")
        for image in ("python.exe", "py.exe", "pythonw.exe"):
            self.assertIn(f"taskkill /F /IM {image}", script)

    def test_restart_retries_trueface_when_missing(self):
        script = _read("restart_all.bat")
        self.assertIn(":verify_trueface", script)
        self.assertIn("run_trueface.bat", script)

    def test_restart_reruns_itself_from_temp_before_pulling(self):
        script = _read("restart_all.bat")
        rerun = script.index('call "!SELF_COPY!" --from-temp')
        pull = script.index("git reset --hard origin/main")
        self.assertLess(rerun, pull)
        self.assertIn('if /I "%~1"=="--from-temp" (\n    cd /d "%~2"', script)

    def test_wrapper_does_not_gate_the_poller_behind_a_file_lock(self):
        script = _read("run_trueface.bat")
        self.assertNotIn("9>", script.split(":run", 1)[0])
        self.assertIn("WRAPPER: launched", script)
        self.assertIn("\ncall :run\n", script)

    def test_watchdog_kills_a_wedged_poller_with_a_stale_log(self):
        script = _read("watchdog.bat")
        self.assertIn("AddMinutes(-15)", script)
        stale_kill = script.index("poller is wedged")
        restart = script.index('if "%NEED_TRUEFACE%"=="1" (')
        self.assertLess(stale_kill, restart)

    def test_autostart_task_starts_every_process(self):
        script = _read("install_autostart.bat")
        self.assertNotIn("run_hidden.vbs", script)
        self.assertEqual(script.count("run_watchdog_hidden.vbs"), 5)

    def test_watchdog_detects_launcher_hosted_processes(self):
        script = _read("watchdog.bat")
        self.assertEqual(
            script.count("$_.Name -in @('python.exe','py.exe','pythonw.exe')"), 3
        )


if __name__ == "__main__":
    unittest.main()
