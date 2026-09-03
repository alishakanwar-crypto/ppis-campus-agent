"""The agent must pick up merged fixes without anyone restarting the PC."""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class _Fetch:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


class PendingUpdateTests(unittest.TestCase):
    def setUp(self):
        main._auto_update_state.update(
            origin_commit="", last_error="", checked_at_ist=""
        )

    def test_no_update_when_running_the_remote_commit(self):
        with patch.object(main.subprocess, "run", return_value=_Fetch()), \
                patch.object(main, "_git", side_effect=["abc1234", "abc1234"]):
            self.assertEqual(main._pending_update_commit(), "")

    def test_reports_the_commit_waiting_on_origin_main(self):
        with patch.object(main.subprocess, "run", return_value=_Fetch()), \
                patch.object(main, "_git", side_effect=["def5678", "abc1234"]):
            self.assertEqual(main._pending_update_commit(), "def5678")

    def test_a_failed_fetch_never_looks_like_an_update(self):
        with patch.object(main.subprocess, "run", return_value=_Fetch()), \
                patch.object(main, "_git", side_effect=["", "abc1234"]):
            self.assertEqual(main._pending_update_commit(), "")

    def test_fetches_before_comparing(self):
        with patch.object(
            main.subprocess, "run", return_value=_Fetch()
        ) as run, patch.object(main, "_git", side_effect=["a", "a"]):
            main._pending_update_commit()
        self.assertEqual(
            run.call_args.args[0], ["git", "fetch", "origin", "main"]
        )

    def test_an_unreachable_github_is_reported_not_swallowed(self):
        """A campus PC that cannot fetch looks up to date, so the cloud has to
        be told, or merged fixes sit unused while everything reads healthy."""
        with patch.object(
            main.subprocess,
            "run",
            return_value=_Fetch(128, "fatal: could not read Username"),
        ), patch.object(main, "_git") as git:
            self.assertEqual(main._pending_update_commit(), "")

        git.assert_not_called()
        state = main.auto_update_state()
        self.assertIn("could not read Username", state["last_error"])
        self.assertTrue(state["checked_at_ist"].endswith("IST"))

    def test_a_crashed_fetch_is_reported_too(self):
        with patch.object(
            main.subprocess, "run", side_effect=OSError("git missing")
        ):
            self.assertEqual(main._pending_update_commit(), "")

        self.assertIn("git missing", main.auto_update_state()["last_error"])

    def test_a_working_check_clears_the_last_error(self):
        main._auto_update_state["last_error"] = "fatal: could not read Username"
        with patch.object(main.subprocess, "run", return_value=_Fetch()), \
                patch.object(main, "_git", side_effect=["abc1234", "abc1234"]):
            main._pending_update_commit()

        state = main.auto_update_state()
        self.assertEqual(state["last_error"], "")
        self.assertEqual(state["origin_commit"], "abc1234")


class PullMergedCodeTests(unittest.TestCase):
    def test_takes_the_merged_code_and_confirms_head_moved(self):
        with patch.object(
            main.subprocess, "run", return_value=_Fetch()
        ) as run, patch.object(main, "_git", side_effect=["old1234", "new1234"]):
            self.assertEqual(main._pull_merged_code("new1234"), "")

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["git", "fetch", "origin", "main"],
                ["git", "reset", "--hard", "FETCH_HEAD"],
            ],
        )

    def test_a_checkout_that_did_not_move_is_reported_as_a_failure(self):
        with patch.object(main.subprocess, "run", return_value=_Fetch()), \
                patch.object(main, "_git", side_effect=["old1234", "old1234"]):
            self.assertIn("still on old1234", main._pull_merged_code("new1234"))

    def test_a_refused_reset_is_reported_and_stops_the_update(self):
        with patch.object(
            main.subprocess,
            "run",
            side_effect=[_Fetch(), _Fetch(1, "error: Your local changes")],
        ), patch.object(main, "_git", return_value="old1234"):
            self.assertIn("local changes", main._pull_merged_code("new1234"))

    def test_a_crashed_git_is_reported_instead_of_raising(self):
        with patch.object(
            main.subprocess, "run", side_effect=OSError("git missing")
        ), patch.object(main, "_git", return_value="old1234"):
            self.assertIn("git missing", main._pull_merged_code("new1234"))


class AutoUpdateLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _run_one_pass(self):
        """Run the loop until it either exits the process or sleeps twice."""
        sleeps = 0

        async def fake_sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        with patch.object(main.asyncio, "sleep", fake_sleep), \
                self.assertRaises(asyncio.CancelledError):
            await main._auto_update_loop()

    async def test_restarts_onto_the_merged_commit_when_idle(self):
        with patch.object(main, "_STARTED_BY_WRAPPER", True), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(main, "_live_requests_in_flight", 0), \
                patch.object(main, "_pending_update_commit", return_value="new1234"), \
                patch.object(main, "_pull_merged_code", return_value="") as pull, \
                patch.object(main.os, "_exit") as exit_now, \
                patch.object(main.logging, "shutdown"):
            await self._run_one_pass()
        pull.assert_called_once_with("new1234")
        exit_now.assert_called_once_with(0)

    async def test_no_restart_when_the_code_did_not_actually_move(self):
        """Restarting without the fix is a restart every ten minutes, nothing more."""
        with patch.object(main, "_STARTED_BY_WRAPPER", True), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(main, "_live_requests_in_flight", 0), \
                patch.object(main, "_pending_update_commit", return_value="new1234"), \
                patch.object(
                    main, "_pull_merged_code", return_value="still on old1234"
                ), \
                patch.object(main.os, "_exit") as exit_now:
            await self._run_one_pass()
        exit_now.assert_not_called()
        self.assertIn("still on old1234", main.auto_update_state()["last_error"])

    async def test_waits_while_a_parent_request_is_being_served(self):
        with patch.object(main, "_STARTED_BY_WRAPPER", True), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(main, "_live_requests_in_flight", 1), \
                patch.object(main, "_pending_update_commit", return_value="new1234"), \
                patch.object(main.os, "_exit") as exit_now:
            await self._run_one_pass()
        exit_now.assert_not_called()

    async def test_stays_put_when_already_on_the_latest_commit(self):
        with patch.object(main, "_STARTED_BY_WRAPPER", True), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(main, "_pending_update_commit", return_value=""), \
                patch.object(main.os, "_exit") as exit_now:
            await self._run_one_pass()
        exit_now.assert_not_called()

    async def test_never_exits_when_no_wrapper_would_restart_us(self):
        with patch.object(main, "_STARTED_BY_WRAPPER", False), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(main, "_pending_update_commit", return_value="new1234"), \
                patch.object(main.os, "_exit") as exit_now:
            await main._auto_update_loop()
        exit_now.assert_not_called()

    async def test_no_exit_while_any_work_is_in_flight(self):
        with patch.object(main, "_STARTED_BY_WRAPPER", True), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(
                    main, "_work_in_flight", return_value="1 attendance job(s)"
                ), \
                patch.object(
                    main, "_pending_update_commit", return_value="new1234"
                ), \
                patch.object(main.os, "_exit") as exit_now:
            await self._run_one_pass()
        exit_now.assert_not_called()

    async def test_the_work_holding_a_fix_back_is_published(self):
        """Health has to name it, or old code looks like healthy code."""
        with patch.object(main, "_STARTED_BY_WRAPPER", True), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(
                    main, "_work_in_flight", return_value="1 queued snapshot(s)"
                ), \
                patch.object(
                    main, "_pending_update_commit", return_value="new1234"
                ), \
                patch.object(main.os, "_exit") as exit_now:
            await self._run_one_pass()

        exit_now.assert_not_called()
        state = main.auto_update_state()
        self.assertEqual(state["held_by"], "1 queued snapshot(s)")
        self.assertTrue(state["held_since_ist"].endswith("IST"))

    async def test_stuck_work_cannot_hold_a_fix_back_all_day(self):
        """A photo takes seconds and a job minutes; anything longer is stuck."""
        with patch.object(main, "_STARTED_BY_WRAPPER", True), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(main, "_AUTO_UPDATE_MAX_HOLD_SECONDS", 0.0), \
                patch.object(
                    main, "_work_in_flight", return_value="1 queued snapshot(s)"
                ), \
                patch.object(
                    main, "_pending_update_commit", return_value="new1234"
                ), \
                patch.object(main, "_pull_merged_code", return_value="") as pull, \
                patch.object(main.os, "_exit") as exit_now, \
                patch.object(main.logging, "shutdown"):
            await self._run_one_pass()

        pull.assert_called_once_with("new1234")
        exit_now.assert_called_once_with(0)

    async def test_a_broken_git_check_does_not_kill_the_loop(self):
        with patch.object(main, "_STARTED_BY_WRAPPER", True), \
                patch.object(main, "_AUTO_UPDATE_ENABLED", True), \
                patch.object(
                    main, "_pending_update_commit", side_effect=OSError("no git")
                ), \
                patch.object(main.os, "_exit") as exit_now:
            await self._run_one_pass()
        exit_now.assert_not_called()


class WorkInFlightTests(unittest.TestCase):
    def setUp(self):
        main._snapshot_tasks.clear()

    def test_idle_agent_reports_nothing_in_flight(self):
        with patch.object(main, "_live_requests_in_flight", 0), \
                patch.object(
                    main.attendance_engine, "work_in_flight", return_value=0
                ):
            self.assertEqual(main._work_in_flight(), "")

    def test_a_snapshot_accepted_but_not_yet_counted_holds_the_update(self):
        task = MagicMock()
        task.done.return_value = False
        main._snapshot_tasks.add(task)
        self.addCleanup(main._snapshot_tasks.discard, task)
        with patch.object(main, "_live_requests_in_flight", 0), \
                patch.object(
                    main.attendance_engine, "work_in_flight", return_value=0
                ):
            self.assertIn("queued snapshot", main._work_in_flight())

    def test_attendance_recognition_holds_the_update(self):
        with patch.object(main, "_live_requests_in_flight", 0), \
                patch.object(
                    main.attendance_engine, "work_in_flight", return_value=2
                ):
            self.assertIn("attendance job", main._work_in_flight())


class WrapperContractTests(unittest.TestCase):
    def test_run_forever_marks_the_agent_as_wrapper_started(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "run_forever.bat")) as handle:
            script = handle.read()
        self.assertIn("set PPIS_WRAPPER=1", script)


if __name__ == "__main__":
    unittest.main()
