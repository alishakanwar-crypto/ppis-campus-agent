"""Best-effort Windows process-priority helpers."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("ppis-agent.process_priority")


def set_windows_process_priority(priority: str, label: str) -> bool:
    """Set this process's Windows priority without making it a hard dependency."""
    if os.name != "nt":
        return False

    try:
        import psutil

        priority_class = getattr(psutil, priority)
        psutil.Process().nice(priority_class)
        logger.info("%s process priority set to %s", label, priority)
        return True
    except Exception as exc:
        logger.warning(
            "Could not set %s process priority to %s: %s",
            label,
            priority,
            exc,
        )
        return False
