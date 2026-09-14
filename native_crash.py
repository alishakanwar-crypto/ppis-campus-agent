"""Keep a process that Windows keeps killing from taking parents with it.

A crash inside a native face library does not raise, log or unwind: Windows
ends the process and the wrapper starts another one, which reaches the same
code and dies again. While that loop runs the cloud link is up for seconds at
a time, so a parent asking for a photo is told the agent is offline.

So a run that follows such a crash starts without the background face work —
the teacher sighting scan, the mood watcher and the classwise attendance
scan — and serves parents only. A run that stays up long enough to be called
healthy clears the count, and the background work returns by itself on the
next start. Nothing here may stop the agent from starting.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent
CRASH_COUNT = _HERE / "native_crash_streak.txt"

# Windows reports a killed process as an NTSTATUS error: 0xC0000005 access
# violation, 0xC0000374 heap corruption, 0xC00000FD stack overflow. A Python
# exit is a small number, and a plain failure (-1, 0xFFFFFFFF) sits above the
# error band, so the band itself is what is read.
_NTSTATUS_FLOOR = 0xC0000000
_NTSTATUS_CEILING = 0xCFFFFFFF

# Two crashes in a row is a loop; one can be a machine hiccup and should not
# cost the school its attendance scan.
CRASHES_BEFORE_PAUSE = 2

# A run that lasts this long did not die on the face work it just did.
STABLE_AFTER_SECONDS = 20 * 60


def killed_by_windows(exit_code: str | int) -> bool:
    """Whether that exit code is Windows ending the process itself."""
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        return False
    if code >= 0:
        return False
    unsigned = code + (1 << 32)
    return _NTSTATUS_FLOOR <= unsigned <= _NTSTATUS_CEILING


def _read_streak() -> int:
    try:
        return max(0, int(CRASH_COUNT.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def _write_streak(value: int) -> None:
    try:
        CRASH_COUNT.write_text(str(value), encoding="utf-8")
    except OSError:
        pass


def note_previous_exit(exit_code: str | int) -> int:
    """Count this start against the crash before it, and say how many.

    Called once, before anything heavy, so the run knows what it is walking
    into. A start that follows an ordinary exit clears the count.
    """
    if killed_by_windows(exit_code):
        streak = _read_streak() + 1
        _write_streak(streak)
        return streak
    _write_streak(0)
    return 0


def mark_stable() -> None:
    """This run lasted; the next one need not hold anything back."""
    _write_streak(0)


def background_face_work_paused(streak: int) -> bool:
    return streak >= CRASHES_BEFORE_PAUSE
