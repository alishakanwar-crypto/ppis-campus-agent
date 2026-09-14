"""The guard that keeps a Windows crash loop from costing parents their photos."""

import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import native_crash


class NativeCrashGuardTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        native_crash.CRASH_COUNT = Path(self._dir.name) / "native_crash_streak.txt"

    def tearDown(self):
        importlib.reload(native_crash)

    def test_windows_fatal_codes_are_recognised(self):
        self.assertTrue(native_crash.killed_by_windows(-1073741819))  # 0xC0000005
        self.assertTrue(native_crash.killed_by_windows("-1073740940"))  # heap
        self.assertTrue(native_crash.killed_by_windows(-1073741571))  # stack

    def test_ordinary_exits_are_not_crashes(self):
        for code in ("0", "1", "42", "-1", "", "nothing", None):
            self.assertFalse(native_crash.killed_by_windows(code), code)

    def test_a_plain_failure_is_not_a_crash(self):
        # -1 is 0xFFFFFFFF, which is a process saying it failed, not Windows
        # ending it, and must not cost the school its attendance scan.
        self.assertFalse(native_crash.killed_by_windows("-1"))
        self.assertFalse(native_crash.killed_by_windows("-107374181"))

    def test_one_crash_does_not_stop_the_background_work(self):
        streak = native_crash.note_previous_exit("-1073741819")
        self.assertEqual(streak, 1)
        self.assertFalse(native_crash.background_face_work_paused(streak))

    def test_a_second_crash_in_a_row_pauses_the_background_work(self):
        native_crash.note_previous_exit("-1073741819")
        streak = native_crash.note_previous_exit("-1073741819")
        self.assertEqual(streak, 2)
        self.assertTrue(native_crash.background_face_work_paused(streak))

    def test_an_ordinary_exit_clears_the_count(self):
        native_crash.note_previous_exit("-1073741819")
        native_crash.note_previous_exit("-1073741819")
        streak = native_crash.note_previous_exit("0")
        self.assertEqual(streak, 0)
        self.assertFalse(native_crash.background_face_work_paused(streak))

    def test_a_run_that_lasted_lets_the_work_return(self):
        native_crash.note_previous_exit("-1073741819")
        native_crash.note_previous_exit("-1073741819")
        native_crash.mark_stable()
        self.assertEqual(native_crash.note_previous_exit("-1073741819"), 1)

    def test_an_unreadable_count_file_does_not_raise(self):
        native_crash.CRASH_COUNT.write_text("not a number", encoding="utf-8")
        self.assertEqual(native_crash.note_previous_exit("-1073741819"), 1)

    def test_a_count_that_cannot_be_written_does_not_raise(self):
        native_crash.CRASH_COUNT = Path(self._dir.name) / "no-such-dir" / "c.txt"
        self.assertEqual(native_crash.note_previous_exit("-1073741819"), 1)
        native_crash.mark_stable()


class NativeLockTests(unittest.TestCase):
    def test_no_second_thread_enters_a_native_call(self):
        import threading

        import face_native

        entered = threading.Event()
        other_got_in = []

        class FakeApp:
            def get(self, image):
                entered.set()
                time.sleep(0.2)
                return ["face"]

        def rival():
            entered.wait(2)
            other_got_in.append(
                face_native.NATIVE_LOCK.acquire(blocking=False)
            )
            if other_got_in[-1]:
                face_native.NATIVE_LOCK.release()

        thread = threading.Thread(target=rival)
        thread.start()
        self.assertEqual(face_native.insight_get(FakeApp(), None), ["face"])
        thread.join(3)
        self.assertEqual(other_got_in, [False])


if __name__ == "__main__":
    unittest.main()
