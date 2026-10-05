"""Free space on the campus PC, and the agent's own share of what fills it.

A Windows process that runs out of disk dies with exit code 112 and leaves no
traceback, which is exactly how the agent has been ending its mornings. Three
of the campus processes also log to files nothing ever trims, so the agent
measures the drive, trims its own logs, and sends the number to the cloud.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Logs written by processes that never rotated them. The agent's own log is
# already capped by its RotatingFileHandler.
UNROTATED_LOGS = (
    "gate_counter.log",
    "trueface_poller.log",
    "chairman_mood.log",
    "wrapper_campus.log",
    "wrapper_trueface.log",
    "wrapper_gate_counter.log",
    "startup_diag.log",
)

# How much of an unrotated log is worth keeping: enough to read the last
# crash, little enough that a year of them cannot fill a disk.
KEEP_LOG_BYTES = 5 * 1024 * 1024

# Below this the agent is in danger of dying the way exit code 112 dies.
LOW_FREE_MB = 2 * 1024

# Where gate counting keeps its copy of the CP Plus video. Resolved exactly as
# gate_counter resolves it -- a relative override against the working
# directory, not against this file -- so the folder measured is the folder
# written to even when the two processes were started from different places.
_GATE_RECORDING_OVERRIDE = os.environ.get("CPPLUS_LOCAL_RECORDING_DIR")
GATE_RECORDING_DIR = str(
    Path(_GATE_RECORDING_OVERRIDE).resolve()
    if _GATE_RECORDING_OVERRIDE
    else _HERE / "cpplus_recordings"
)


def _free_and_total_mb() -> tuple[float | None, float | None]:
    try:
        usage = shutil.disk_usage(_HERE)
    except OSError:
        return None, None
    return (
        round(usage.free / (1024 * 1024), 1),
        round(usage.total / (1024 * 1024), 1),
    )


def trim_oversized_logs() -> list[str]:
    """Keep only the tail of each unrotated log; return the ones trimmed."""
    trimmed: list[str] = []
    for name in UNROTATED_LOGS:
        path = _HERE / name
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= KEEP_LOG_BYTES * 2:
            continue
        kept_from = size - KEEP_LOG_BYTES
        try:
            with path.open("rb") as handle:
                handle.seek(kept_from)
                tail = handle.read()
            # Written in place: another process may be holding the file open,
            # and on Windows a rename would fail where a write succeeds.
            with path.open("r+b") as handle:
                # The owning process keeps appending while this runs, so take
                # whatever arrived since the tail was read before overwriting.
                handle.seek(kept_from + len(tail))
                tail += handle.read()
                handle.seek(0)
                handle.write(tail)
                handle.truncate(len(tail))
            trimmed.append(f"{name} ({round(size / (1024 * 1024))} MB)")
        except OSError:
            continue
    return trimmed


def _folder_mb(*folders: str | Path) -> float:
    total = 0.0
    for folder in folders:
        base = Path(folder)
        if not base.is_absolute():
            base = _HERE / base
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size / (1024 * 1024)
            except OSError:
                continue
    return total


def disk_health() -> dict:
    """What the cloud needs to see a disk filling up before it kills us."""
    free_mb, total_mb = _free_and_total_mb()
    logs_mb = 0.0
    for path in list(_HERE.glob("*.log")) + list(_HERE.glob("*.log.*")):
        try:
            logs_mb += path.stat().st_size / (1024 * 1024)
        except OSError:
            continue
    snapshots_mb = _folder_mb("snapshots", "attendance_snapshots", "face_images")
    # Gate counting keeps its own copy of the CP Plus video it replays, which
    # is the largest thing the agent writes when it is enabled.
    recordings_mb = _folder_mb(GATE_RECORDING_DIR)
    return {
        "drive": str(_HERE.drive or os.sep),
        "free_mb": free_mb,
        "total_mb": total_mb,
        "low": bool(free_mb is not None and free_mb < LOW_FREE_MB),
        "logs_mb": round(logs_mb, 1),
        "snapshots_mb": round(snapshots_mb, 1),
        "recordings_mb": round(recordings_mb, 1),
    }
