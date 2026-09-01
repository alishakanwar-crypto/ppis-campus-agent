"""Recorder login state shared by every process on the campus PC.

The campus agent, the gate counter and the mood watcher are separate processes
that each log in to the same recorders. A Hikvision admin account stays locked
for as long as anything keeps presenting the rejected password, so one process
backing off achieves nothing while another keeps knocking — which is exactly
how DVR 2 stayed locked for a whole day.

This module is the one place that answers "has this recorder refused us, and
when did anybody last try it?", using a small JSON file so the answer survives
both process boundaries and restarts. Every write is atomic and every failure
is swallowed: a recorder must never go dark because bookkeeping broke.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("ppis-agent")

STATE_PATH = Path(
    os.environ.get(
        "RECORDER_AUTH_STATE",
        str(Path(__file__).resolve().parent / ".locks" / "recorder_auth.json"),
    )
)

# How long a recorder must be left completely alone before one probe is allowed.
QUIET_SECONDS = max(
    300.0, float(os.environ.get("ISAPI_AUTH_UNLOCK_QUIET_SECONDS", "1800"))
)
MAX_QUIET_SECONDS = max(
    QUIET_SECONDS,
    float(os.environ.get("ISAPI_AUTH_UNLOCK_MAX_QUIET_SECONDS", "14400")),
)


def credential_key(username: str, password: str) -> str:
    """Fingerprint of a recorder's login, never the login itself."""
    raw = f"{username or ''}:{password or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _read() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        # Bookkeeping must never stop a camera from being captured.
        return {}


def _write(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(STATE_PATH.parent),
            delete=False,
        ) as handle:
            json.dump(state, handle)
            temp_name = handle.name
        os.replace(temp_name, STATE_PATH)
    except (OSError, ValueError):
        logger.debug("could not persist recorder auth state", exc_info=True)


def _entry(ip: str) -> dict:
    entry = _read().get(ip)
    return entry if isinstance(entry, dict) else {}


def _update(ip: str, **fields) -> dict:
    state = _read()
    entry = state.get(ip)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(fields)
    state[ip] = entry
    _write(state)
    return entry


def note_attempt(ip: str) -> None:
    """Record that some process just presented a login to this recorder."""
    _update(ip, last_attempt=time.time())


def note_success(ip: str) -> None:
    """The recorder accepted us, so nothing about it is locked any more."""
    now = time.time()
    entry = _entry(ip)
    if (
        entry.get("refused_at") is None
        and entry.get("last_success")
        and now - float(entry["last_success"]) < 60
    ):
        # Every gate frame succeeds; rewriting the file each time is waste.
        return
    state = _read()
    state[ip] = {"last_success": now, "last_attempt": now}
    _write(state)


def seconds_since_success(ip: str) -> float | None:
    """How long ago this recorder last served us, None when never."""
    last = _entry(ip).get("last_success")
    if not last:
        return None
    return max(0.0, time.time() - float(last))


def recently_worked(ip: str, within_seconds: float = 900.0) -> bool:
    """Whether the recorder served us recently enough to trust the login."""
    since = seconds_since_success(ip)
    return since is not None and since <= within_seconds


def note_refusal(ip: str, credential_key: str = "") -> None:
    """The recorder rejected our login: hold every process off it."""
    entry = _entry(ip)
    now = time.time()
    quiet = float(entry.get("quiet_seconds") or QUIET_SECONDS)
    _update(
        ip,
        refused_at=entry.get("refused_at") or now,
        last_success=None,
        credential_key=credential_key,
        last_attempt=now,
        quiet_seconds=quiet,
        next_probe_at=now + quiet,
    )


def note_probe_failed(ip: str) -> float:
    """A probe was refused: double the silence, capped, and return it."""
    entry = _entry(ip)
    quiet = min(
        MAX_QUIET_SECONDS,
        float(entry.get("quiet_seconds") or QUIET_SECONDS) * 2,
    )
    now = time.time()
    _update(
        ip,
        quiet_seconds=quiet,
        last_attempt=now,
        next_probe_at=now + quiet,
    )
    return quiet


def clear(ip: str) -> None:
    """Forget a refusal without claiming the recorder just served us."""
    state = _read()
    entry = state.get(ip)
    if not isinstance(entry, dict) or not entry.get("refused_at"):
        return
    for field in ("refused_at", "credential_key", "quiet_seconds",
                  "next_probe_at"):
        entry.pop(field, None)
    state[ip] = entry
    _write(state)


def is_refused(ip: str, credential_key: str = "") -> bool:
    """Whether any process has been refused by this recorder's login."""
    entry = _entry(ip)
    if not entry.get("refused_at"):
        return False
    known = entry.get("credential_key") or ""
    if credential_key and known and credential_key != known:
        # A different password is in use now, so the old refusal says nothing.
        clear(ip)
        return False
    return True


def seconds_since_attempt(ip: str) -> float | None:
    """How long this recorder has been left alone, None when never tried."""
    last = _entry(ip).get("last_attempt")
    if not last:
        return None
    return max(0.0, time.time() - float(last))


def quiet_seconds(ip: str) -> float:
    return float(_entry(ip).get("quiet_seconds") or QUIET_SECONDS)


def probe_due(ip: str) -> bool:
    """True when the recorder has been silent long enough for one probe."""
    entry = _entry(ip)
    if not entry.get("refused_at"):
        return False
    due_at = entry.get("next_probe_at")
    if due_at is None or time.time() < float(due_at):
        return False
    quiet = float(entry.get("quiet_seconds") or QUIET_SECONDS)
    since = seconds_since_attempt(ip)
    return since is None or since >= quiet


def seconds_until_probe(ip: str) -> float | None:
    """Seconds before the single unlock probe, None when none is scheduled."""
    entry = _entry(ip)
    if not entry.get("refused_at"):
        return None
    due_at = entry.get("next_probe_at")
    if due_at is None:
        return None
    quiet = float(entry.get("quiet_seconds") or QUIET_SECONDS)
    last = entry.get("last_attempt")
    if last:
        due_at = max(float(due_at), float(last) + quiet)
    return max(0.0, float(due_at) - time.time())
