"""Windows single-instance guard for the TrueFace poller."""

from __future__ import annotations

import atexit
import logging
import os

logger = logging.getLogger("trueface_poller")

_MUTEX_NAMES = (
    r"Global\PPIS.TrueFacePoller",
    r"Local\PPIS.TrueFacePoller",
)
_ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def _other_poller_process_exists() -> bool:
    """Fallback duplicate check if the Windows mutex API is unavailable."""
    try:
        import psutil

        current_pid = os.getpid()
        for process in psutil.process_iter(["pid", "cmdline"]):
            if process.info["pid"] == current_pid:
                continue
            command_line = " ".join(process.info.get("cmdline") or []).lower()
            if "trueface_poller.py" in command_line:
                return True
    except Exception as exc:
        logger.warning("Fallback TrueFace process check failed: %s", exc)
    return False


def acquire_single_instance() -> bool:
    """Acquire a kernel-backed mutex; Windows releases it after a hard kill."""
    global _mutex_handle

    if os.name != "nt":
        return True

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool

        for mutex_name in _MUTEX_NAMES:
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, True, mutex_name)
            if not handle:
                continue
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                logger.error(
                    "Another TrueFace poller instance is already running "
                    "(mutex %s); exiting without starting Chrome.",
                    mutex_name,
                )
                return False
            _mutex_handle = (kernel32, handle)
            atexit.register(release_single_instance)
            logger.info("Acquired TrueFace poller instance mutex %s", mutex_name)
            return True
    except Exception as exc:
        logger.warning("Windows mutex guard unavailable: %s", exc)

    if _other_poller_process_exists():
        logger.error(
            "Another TrueFace poller instance is already running "
            "(fallback process check); exiting."
        )
        return False

    logger.warning(
        "Could not establish the Windows mutex guard; no duplicate process "
        "was found, so continuing with the fallback check."
    )
    return True


def release_single_instance() -> None:
    """Release the mutex on normal shutdown; Windows also releases it on exit."""
    global _mutex_handle
    if _mutex_handle is None:
        return
    kernel32, handle = _mutex_handle
    try:
        kernel32.CloseHandle(handle)
    except Exception:
        pass
    _mutex_handle = None
