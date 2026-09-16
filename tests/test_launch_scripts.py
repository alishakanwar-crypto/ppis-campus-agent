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
        self.assertIn(":retry_trueface", script)
        self.assertIn("run_trueface.bat", script)

    def test_restart_claims_success_only_after_waiting_for_each_agent(self):
        # A fixed sleep let the script print "all 3 started" while the poller
        # was still coming up, so a morning went by with no attendance.
        script = _read("restart_all.bat")
        for name in ("main.py", "trueface_poller", "gate_counter"):
            self.assertIn(f'call :wait_for "{name}"', script)
        retry = script.index("goto check_agents")
        claim = script.index("[OK] All 3 agents started successfully")
        self.assertLess(claim, retry)

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

    def test_watchdog_agent_only_mode_leaves_the_desktop_agents_alone(self):
        script = _read("watchdog.bat")
        self.assertIn('if /i "%~1"=="agent-only" set AGENT_ONLY=1', script)
        # Every TrueFace and gate counter check sits behind the normal mode.
        for guarded in (
            'if "%AGENT_ONLY%"=="0" (\n    powershell',
            'if "%AGENT_ONLY%"=="0" if "%NEED_TRUEFACE%"=="0" (',
        ):
            self.assertIn(guarded, script)

    def test_autostart_task_starts_every_process(self):
        script = _read("install_autostart.bat")
        self.assertNotIn("run_hidden.vbs", script)
        self.assertEqual(script.count("run_watchdog_hidden.vbs"), 5)

    def test_installer_echoes_inside_blocks_cannot_close_the_block(self):
        # An unescaped ')' in `echo ... (XML method)` ended the if-block early,
        # so the fallback ran too and overwrote the XML-defined task.
        for line in _read("install_autostart.bat").splitlines():
            if line.startswith((" ", "\t")) and line.strip().startswith("echo "):
                self.assertNotIn(")", line, line)

    def test_installer_replaces_legacy_duplicate_tasks(self):
        script = _read("install_autostart.bat")
        for task in ("PPIS Agent Autostart", "PPIS TrueFace Poller"):
            self.assertIn(f'schtasks /delete /tn "{task}" /f', script)
        self.assertIn("nightly_restart.bat", script)
        self.assertIn("/sc daily /st 03:00", script)

    def test_nightly_restart_is_unattended_and_pull_safe(self):
        script = _read("nightly_restart.bat")
        self.assertNotIn("\npause", script)
        rerun = script.index('call "!SELF_COPY!" --from-temp')
        pull = script.index("reset --hard origin/main")
        self.assertLess(rerun, pull)
        self.assertIn('call "!AGENT_DIR!watchdog.bat"', script)

    def test_nightly_restart_starts_only_the_agent_as_system(self):
        # TrueFace needs a Chrome window and the gate counter needs native
        # CP Plus: neither can start in session 0, so under SYSTEM only the
        # campus agent is started and parents keep their photos.
        script = _read("nightly_restart.bat")
        self.assertIn("S-1-5-18", script)
        self.assertIn("WATCH_MODE=agent-only", script)
        self.assertIn('call "!AGENT_DIR!watchdog.bat" !WATCH_MODE!', script)

    def test_nightly_refresh_owns_the_checkout_it_updates(self):
        # As SYSTEM the checkout belongs to somebody else, and git then calls
        # it an unsafe repository, so the refresh would restart the agent on
        # last night's code without either command being seen to fail.
        script = _read("nightly_restart.bat")
        # Quoted: a campus path with a space in it must reach git whole.
        self.assertIn('set OWNED=-c "safe.directory=!REPO!"', script)
        for command in ("fetch origin", "reset --hard origin/main"):
            self.assertIn(f"git !OWNED! {command}", script)

    def test_a_failed_nightly_refresh_ends_the_task_non_zero(self):
        script = _read("nightly_restart.bat")
        self.assertEqual(script.count('if errorlevel 1 set "REFRESHED="'), 2)
        # The agents are started before the script gives up, so a failed
        # refresh never leaves the campus without an agent overnight.
        start = script.index('call "!AGENT_DIR!watchdog.bat" !WATCH_MODE!')
        give_up = script.index("if not defined REFRESHED (\n    endlocal")
        self.assertLess(start, give_up)
        self.assertIn("exit /b 1", script[give_up:])

    def test_the_wrapper_owns_the_checkout_it_updates(self):
        # The SYSTEM watchdog starts this wrapper with nobody logged on.
        script = _read("run_forever.bat")
        self.assertIn('set OWNED=-c "safe.directory=!REPO!"', script)
        self.assertNotIn("\ngit fetch", script)
        for command in (
            "fetch origin",
            "checkout main",
            "reset --hard origin/main",
            "rev-parse --short HEAD",
        ):
            self.assertIn(f"git !OWNED! {command}", script)

    def test_wrapper_takes_over_when_the_mutex_holder_has_gone(self):
        # Exiting on a refused mutex left the campus with no agent at all for
        # an hour, because the process holding it was already on its way out.
        script = _read("run_forever.bat")
        block = script.split('if "%EXIT_CODE%"=="%DUPLICATE_EXIT_CODE%" (', 1)[1]
        block = block.split("\n)", 1)[0]
        self.assertIn("another_agent_is_running", block)
        self.assertIn("goto loop", block)
        self.assertLess(block.index("goto loop"), block.index("exit /b 0"))

    def test_watchdog_detects_launcher_hosted_processes(self):
        script = _read("watchdog.bat")
        self.assertEqual(
            script.count("$_.Name -in @('python.exe','py.exe','pythonw.exe')"), 3
        )


if __name__ == "__main__":
    unittest.main()
