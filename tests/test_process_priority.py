import logging
import sys
import unittest
from unittest.mock import patch

import campus_instance
import process_priority
import trueface_instance
import trueface_poller


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
    def test_duplicate_polling_exits_with_dedicated_code(self):
        args = type("Args", (), {"test": False})()
        with patch.object(trueface_poller, "acquire_single_instance", return_value=False):
            with self.assertRaises(SystemExit) as raised:
                trueface_poller._run_from_args(args)
        self.assertEqual(
            raised.exception.code,
            trueface_instance.DUPLICATE_INSTANCE_EXIT_CODE,
        )

    def test_connectivity_test_is_not_blocked_by_running_poller(self):
        args = type("Args", (), {"test": True})()
        with (
            patch.object(trueface_poller, "test_connectivity") as test_connectivity,
            patch.object(
                trueface_poller,
                "acquire_single_instance",
                side_effect=AssertionError("test mode must not acquire mutex"),
            ),
        ):
            trueface_poller._run_from_args(args)
        test_connectivity.assert_called_once_with()

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

    def test_campus_global_mutex_failure_checks_for_duplicate_before_local_acceptance(self):
        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class FakeCtypes:
            c_void_p = object
            c_bool = object
            c_wchar_p = object

            def __init__(self):
                self.last_error = 0
                self.calls = 0
                self.kernel = type("Kernel", (), {})()
                self.kernel.CreateMutexW = FakeFunction(self.create_mutex)
                self.kernel.CloseHandle = FakeFunction(lambda _handle: True)

            def create_mutex(self, *_args):
                self.calls += 1
                self.last_error = 5 if self.calls == 1 else 0
                return None if self.calls == 1 else 401

            def WinDLL(self, *_args, **_kwargs):
                return self.kernel

            def get_last_error(self):
                return self.last_error

            def set_last_error(self, value):
                self.last_error = value

        fake_ctypes = FakeCtypes()
        with (
            patch.object(campus_instance.os, "name", "nt"),
            patch.dict(sys.modules, {"ctypes": fake_ctypes}),
            patch.object(campus_instance, "_mutex_handle", None),
            patch.object(
                campus_instance,
                "_other_agent_process_exists",
                return_value=True,
            ) as scan,
            self.assertLogs(campus_instance.logger, logging.WARNING) as logs,
        ):
            self.assertFalse(campus_instance.acquire_single_instance())
        scan.assert_called_once_with()
        self.assertIn("Win32 error 5", "\n".join(logs.output))

    def test_campus_global_mutex_failure_allows_local_when_scan_finds_nothing(self):
        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class FakeCtypes:
            c_void_p = object
            c_bool = object
            c_wchar_p = object

            def __init__(self):
                self.last_error = 0
                self.calls = 0
                self.kernel = type("Kernel", (), {})()
                self.kernel.CreateMutexW = FakeFunction(self.create_mutex)
                self.kernel.CloseHandle = FakeFunction(lambda _handle: True)

            def create_mutex(self, *_args):
                self.calls += 1
                self.last_error = 5 if self.calls == 1 else 0
                return None if self.calls == 1 else 401

            def WinDLL(self, *_args, **_kwargs):
                return self.kernel

            def get_last_error(self):
                return self.last_error

            def set_last_error(self, value):
                self.last_error = value

        fake_ctypes = FakeCtypes()
        with (
            patch.object(campus_instance.os, "name", "nt"),
            patch.dict(sys.modules, {"ctypes": fake_ctypes}),
            patch.object(campus_instance, "_mutex_handle", None),
            patch.object(
                campus_instance,
                "_other_agent_process_exists",
                return_value=False,
            ) as scan,
        ):
            self.assertTrue(campus_instance.acquire_single_instance())
            campus_instance.release_single_instance()
        scan.assert_called_once_with()

    def test_global_mutex_failure_checks_for_duplicate_before_local_acceptance(self):
        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class FakeCtypes:
            c_void_p = object
            c_bool = object
            c_wchar_p = object

            def __init__(self):
                self.last_error = 0
                self.calls = 0
                self.kernel = type("Kernel", (), {})()
                self.kernel.CreateMutexW = FakeFunction(self.create_mutex)
                self.kernel.CloseHandle = FakeFunction(lambda _handle: True)

            def create_mutex(self, *_args):
                self.calls += 1
                self.last_error = 5 if self.calls == 1 else 0
                return None if self.calls == 1 else 401

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
            patch.object(
                trueface_instance,
                "_other_poller_process_exists",
                return_value=True,
            ) as scan,
            self.assertLogs(trueface_instance.logger, logging.WARNING) as logs,
        ):
            self.assertFalse(trueface_instance.acquire_single_instance())
        scan.assert_called_once_with()
        self.assertIn("Win32 error 5", "\n".join(logs.output))

    def test_global_mutex_failure_allows_local_when_scan_finds_nothing(self):
        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class FakeCtypes:
            c_void_p = object
            c_bool = object
            c_wchar_p = object

            def __init__(self):
                self.last_error = 0
                self.calls = 0
                self.kernel = type("Kernel", (), {})()
                self.kernel.CreateMutexW = FakeFunction(self.create_mutex)
                self.kernel.CloseHandle = FakeFunction(lambda _handle: True)

            def create_mutex(self, *_args):
                self.calls += 1
                self.last_error = 5 if self.calls == 1 else 0
                return None if self.calls == 1 else 401

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
            patch.object(
                trueface_instance,
                "_other_poller_process_exists",
                return_value=False,
            ) as scan,
        ):
            self.assertTrue(trueface_instance.acquire_single_instance())
            trueface_instance.release_single_instance()
        scan.assert_called_once_with()


class TestCampusInstance(unittest.TestCase):
    def test_duplicate_exit_code_is_distinct_from_crash_code(self):
        self.assertNotEqual(campus_instance.DUPLICATE_INSTANCE_EXIT_CODE, 1)
        self.assertEqual(
            campus_instance.DUPLICATE_INSTANCE_EXIT_CODE,
            trueface_instance.DUPLICATE_INSTANCE_EXIT_CODE,
        )

    def test_mutex_rejects_second_instance(self):
        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class FakeKernel32:
            CreateMutexW = FakeFunction(lambda *_args: 301)
            CloseHandle = FakeFunction(lambda _handle: True)

        class FakeCtypes:
            c_void_p = object
            c_bool = object
            c_wchar_p = object

            CreateMutexError = 183

            def WinDLL(self, *_args, **_kwargs):
                return FakeKernel32()

            def get_last_error(self):
                return self.CreateMutexError

            def set_last_error(self, _value):
                pass

        with (
            patch.object(campus_instance.os, "name", "nt"),
            patch.dict(sys.modules, {"ctypes": FakeCtypes()}),
            patch.object(campus_instance, "_mutex_handle", None),
            patch.object(campus_instance, "_other_agent_process_exists", return_value=False),
            self.assertLogs(campus_instance.logger, logging.ERROR) as logs,
        ):
            self.assertFalse(campus_instance.acquire_single_instance())
        self.assertIn("Another campus agent instance is already running", "\n".join(logs.output))

    def test_mutex_guard_fails_open_when_unavailable(self):
        class BrokenCtypes:
            def WinDLL(self, *_args, **_kwargs):
                raise OSError("kernel32 unavailable")

        with (
            patch.object(campus_instance.os, "name", "nt"),
            patch.dict(sys.modules, {"ctypes": BrokenCtypes()}),
            patch.object(campus_instance, "_other_agent_process_exists", return_value=False),
            self.assertLogs(campus_instance.logger, logging.WARNING) as logs,
        ):
            self.assertTrue(campus_instance.acquire_single_instance())
        self.assertIn("continuing fail-open", "\n".join(logs.output))

    def test_mutex_name_does_not_collide_with_trueface(self):
        self.assertNotEqual(campus_instance._MUTEX_NAMES, trueface_instance._MUTEX_NAMES)

    def test_instance_guard_is_noop_off_windows(self):
        with patch.object(campus_instance.os, "name", "posix"):
            self.assertTrue(campus_instance.acquire_single_instance())
            campus_instance.release_single_instance()

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
