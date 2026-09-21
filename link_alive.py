"""Proof on disk that the cloud can still see this agent.

The watchdog can only see whether a python process exists, and a process that
is alive but whose cloud link is gone looks exactly like a healthy one from
outside: that is how the campus sat unseen from 03:00 to 07:13 with every
scheduled task reporting success and every parent request landing nowhere.

The agent touches this file each time the link really carries traffic, so a
stale file is the one thing an outside watcher can trust: no link for minutes,
whatever the process list says.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

LINK_FILE = Path(__file__).resolve().parent / ".locks" / "cloud_link.alive"

# Writing on every frame would beat the disk for nothing: the watchdog only
# asks every five minutes and judges by minutes, not seconds.
WRITE_EVERY_SECONDS = 30.0

_last_write = 0.0


def note_link_alive(force: bool = False) -> None:
    """Record that the cloud link is carrying traffic right now."""
    global _last_write
    now = time.monotonic()
    if not force and _last_write and now - _last_write < WRITE_EVERY_SECONDS:
        return
    try:
        LINK_FILE.parent.mkdir(parents=True, exist_ok=True)
        LINK_FILE.write_text(
            time.strftime("%d-%m-%Y %H:%M:%S", time.localtime()),
            encoding="utf-8",
        )
        _last_write = now
    except OSError:
        # A photo must never be lost over a bookkeeping file. A file we could
        # not write goes stale, and the watchdog restarts an agent that was
        # working; that costs one restart, where swallowing a dead link costs
        # a morning.
        pass


def seconds_since_link_seen() -> float | None:
    """How long since the link was last proven alive, None if never."""
    try:
        return max(0.0, time.time() - os.path.getmtime(LINK_FILE))
    except OSError:
        return None
