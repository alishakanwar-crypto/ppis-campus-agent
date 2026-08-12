"""Windows single-instance guard for the campus agent."""

from __future__ import annotations

import atexit
import logging
import os

logger = logging.getLogger("campus_agent")

_MUTEX_NAMES = (
    r"Global\PPIS.CampusAgent",
    r"Local\PPIS.CampusAgent",
)
_ERROR_ALREADY_EXISTS = 183
DUPLICATE_INSTANCE_EXIT_CODE = 75
_mutex_handle = None


def _other_agent_process_exists() -> bool:
    """Fallback duplicate check if the Windows mutex API is unavailable."""
    try:
        import psutil

        current_pid = os.getpid()
        for process in psutil.process_iter(["pid", "cmdline"]):
            if process.info["pid"] == current_pid:
                continue
            command_line = " ".join(process.info.get("cmdline") or []).lower()
            if "main.py" in command_line:
                return True
    except Exception as exc:
        logger.warning("Fallback campus-agent process check failed: %s", exc)
    return False


def acquire_single_instance() -> bool:
    """Acquire a kernel-backed mutex; Windows releases it after hard termination."""
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
                    "Another campus agent instance is already running "
                    "(mutex %s); exiting before opening cameras or WebSocket.",
                    mutex_name,
                )
                return False
            _mutex_handle = (kernel32, handle)
            atexit.register(release_single_instance)
            logger.info("Acquired campus-agent instance mutex %s", mutex_name)
            return True
    except Exception as exc:
        logger.warning("Windows campus-agent mutex guard unavailable: %s", exc)

    if _other_agent_process_exists():
        logger.error(
            "Another campus agent instance is already running "
            "(fallback process check); exiting."
        )
        return False

    logger.warning(
        "Could not establish the campus-agent Windows mutex; no duplicate "
        "process was found, so continuing fail-open."
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
