import logging
import sys
import unittest
from unittest.mock import patch

import process_priority
import trueface_instance


class TestProcessPriority(unittest.TestCase):
    def test_priority_setting_is_noop_off_windows(self):
        with patch.object(process_priority.os, "name", "posix"):
            self.assertFalse(
                process_priority.set_windows_process_priority(
                    "ABOVE_NORMAL_PRIORITY_CLASS", "test"
                )
            )

    def test_priority_setting_degrades_when_denied(self):
        class DeniedProcess:
            def nice(self, _priority):
                raise PermissionError("Access is denied")

        class FakePsutil:
            ABOVE_NORMAL_PRIORITY_CLASS = 32768

            @staticmethod
            def Process():
                return DeniedProcess()

        with (
            patch.object(process_priority.os, "name", "nt"),
            patch.dict(sys.modules, {"psutil": FakePsutil}),
            self.assertLogs(process_priority.logger, logging.WARNING) as logs,
        ):
            self.assertFalse(
                process_priority.set_windows_process_priority(
                    "ABOVE_NORMAL_PRIORITY_CLASS", "test"
                )
            )
        self.assertIn("Access is denied", "\n".join(logs.output))


class TestTrueFaceInstance(unittest.TestCase):
    def test_instance_guard_is_noop_off_windows(self):
        with patch.object(trueface_instance.os, "name", "posix"):
            self.assertTrue(trueface_instance.acquire_single_instance())
            trueface_instance.release_single_instance()

    def test_mutex_can_be_reacquired_after_release(self):
        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class FakeKernel32:
            def __init__(self):
                self.handles = iter((101, 102))
                self.CreateMutexW = FakeFunction(
                    lambda *_args: next(self.handles)
                )
                self.CloseHandle = FakeFunction(lambda _handle: True)

        class FakeCtypes:
            c_void_p = object
            c_bool = object
            c_wchar_p = object

            def __init__(self):
                self.kernel = FakeKernel32()
                self.last_error = 0

            def WinDLL(self, *_args, **_kwargs):
                return self.kernel

            def get_last_error(self):
                return self.last_error

            def set_last_error(self, value):
                self.last_error = value

        fake_ctypes = FakeCtypes()
        with (
            patch.object(trueface_instance.os, "name", "nt"),
            patch.dict(sys.modules, {"ctypes": fake_ctypes}),
            patch.object(trueface_instance, "_mutex_handle", None),
        ):
            self.assertTrue(trueface_instance.acquire_single_instance())
            trueface_instance.release_single_instance()
            self.assertTrue(trueface_instance.acquire_single_instance())
            trueface_instance.release_single_instance()
