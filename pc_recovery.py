"""Whether this campus PC can bring the agents back without a person.

Everything that restarts an agent on this PC is a Windows scheduled task, and
a task registered to run as the logged-on user does not run while the PC sits
at the login screen. A night-time reboot then leaves the campus with no agent
until somebody logs in, which is exactly an hour of parents getting nothing
and is invisible from the cloud.

So the agent reads the state of its own tasks at startup and carries it to the
cloud: when the PC last booted, whether each task exists, is enabled, needs a
logon, and how its last run ended. A disabled task is re-enabled here and a
missing logon-free watchdog is installed here, because those repairs need
nobody. Nothing here may keep the agent from starting, and no path or account
name from the campus PC is reported.
"""

import locale
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

# A fixed offset, not a named zone: main.py imports this module before the
# agent starts, and on a Windows PC without the IANA database a named zone
# raises at import and then nothing can start the agent at all. IST keeps no
# daylight saving, so the offset is the whole of it.
IST = timezone(timedelta(hours=5, minutes=30))

# The tasks install_autostart.bat registers. The names are the contract.
BOOT_TASK = "PPIS Campus Agent"
WATCHDOG_TASK = "PPIS Campus Agent Watchdog"
NIGHTLY_TASK = "PPIS Nightly Restart"
# The one task that runs with nobody logged on, and so the only one that can
# keep parents' photos alive through a night-time reboot or crash.
SYSTEM_WATCHDOG_TASK = "PPIS Campus Agent Watchdog (System)"
TASKS = (BOOT_TASK, WATCHDOG_TASK, SYSTEM_WATCHDOG_TASK, NIGHTLY_TASK)

# A task query on a busy PC is slow but never long: the agent is starting and
# parents are waiting, so a query that hangs is abandoned rather than waited on.
_QUERY_TIMEOUT_SECONDS = 20

# Keep schtasks off the screen: the agent runs hidden and a console window
# flashing over a teacher's desktop every start is not acceptable.
if os.name == "nt":
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    _NO_WINDOW = 0

# Only these logon types run without anybody logged on.
_RUNS_WITHOUT_LOGON = {"serviceaccount", "password", "s4u"}

_LOGON_TYPE = re.compile(r"<LogonType>\s*([A-Za-z0-9]+)\s*</LogonType>")
_ENABLED = re.compile(r"<Settings>.*?<Enabled>\s*(true|false)\s*</Enabled>", re.S)

# Task Scheduler's way of saying a task has never run: result 267011 and the
# 30 November 1999 placeholder it prints instead of a time.
_NEVER_RUN_RESULT = "267011"
_NEVER_RUN_STAMP = re.compile(r"\b(?:30[-/.]11|11[-/.]30)[-/.]1999\b")
# 0 is a finished run, 267009 a run still going: both are the task working.
_RAN_WELL = {"0", "0x0", "267009"}


def _ist(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, IST).strftime("%d-%m-%Y %H:%M:%S IST")


def boot_at_ist() -> str:
    """When Windows last started, so a night-time reboot can be seen."""
    try:
        return _ist(psutil.boot_time())
    except Exception as exc:
        logger.debug("PC RECOVERY: boot time unreadable: %s", exc)
        return ""


def _decode(raw: bytes) -> str:
    """schtasks /xml answers in UTF-16; the console pages answer in the OEM
    codepage. Decoding UTF-16 as a byte codepage leaves a NUL between every
    letter, and then <LogonType> is never found and a logon-bound task reads
    as one that needs no logon - the one lie this module must not tell."""
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[1:2] == b"\x00":
        try:
            return raw.decode("utf-16", errors="replace")
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _run(args: list[str]) -> str:
    try:
        done = subprocess.run(
            args,
            capture_output=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:
        logger.debug("PC RECOVERY: %s failed: %s", args[0], exc)
        return ""
    if done.returncode != 0:
        return ""
    return _decode(done.stdout or b"")


def _task_xml(name: str) -> str:
    # A missing task is an error exit and comes back as an empty string.
    return _run(["schtasks", "/query", "/tn", name, "/xml", "ONE"])


def _last_run(name: str) -> dict:
    """The last run time and result, from the list view schtasks prints."""
    out = _run(["schtasks", "/query", "/tn", name, "/fo", "LIST", "/v"])
    found = {"last_run": "", "last_result": "", "next_run": ""}
    for line in out.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label = label.strip().lower()
        value = value.strip()
        if label == "last run time":
            found["last_run"] = value
        elif label == "last result":
            found["last_result"] = value
        elif label == "next run time":
            found["next_run"] = value
    return found


def _install_system_watchdog() -> bool:
    """Register the logon-free watchdog ourselves, so nobody has to.

    The agent runs elevated from its own scheduled task, so it can usually
    create this one, and then a campus PC that reboots at night brings its own
    agent back with nobody logged on. Where it cannot (not elevated), health
    reports the task as missing and install_autostart.bat is the fallback.
    """
    runner = Path(__file__).parent / "run_watchdog_agent_only_hidden.vbs"
    if not runner.exists():
        return False
    _run([
        "schtasks", "/create",
        "/tn", SYSTEM_WATCHDOG_TASK,
        "/tr", f'wscript.exe "{runner}"',
        "/sc", "minute", "/mo", "5",
        "/ru", "SYSTEM", "/rl", "highest", "/f",
    ])
    # Believe only the task's own state after: without administrator rights
    # schtasks refuses SYSTEM and the old task, if any, is left as it was.
    after = _task_state(SYSTEM_WATCHDOG_TASK)
    return bool(after.get("exists")) and not after.get("needs_logon")


def _install_system_nightly() -> bool:
    """Register the 03:00 IST nightly refresh as SYSTEM, so it runs with
    nobody logged on.

    nightly_restart.bat already knows when it is SYSTEM and then starts the
    campus agent alone, because TrueFace's Chrome and the gate counter's native
    window cannot live in session 0. A nightly refresh bound to a logon simply
    does not happen on a night when nobody logged in, and the PC then sits on
    stale code until somebody notices in the morning.
    """
    runner = Path(__file__).parent / "nightly_restart.bat"
    if not runner.exists():
        return False
    _run([
        "schtasks", "/create",
        "/tn", NIGHTLY_TASK,
        "/tr", f'cmd.exe /c "{runner}"',
        "/sc", "daily", "/st", "03:00",
        "/ru", "SYSTEM", "/rl", "highest", "/f",
    ])
    after = _task_state(NIGHTLY_TASK)
    return bool(after.get("exists")) and not after.get("needs_logon")


# The tasks that must never wait for a logon, and how to register each one.
# The boot task and the ordinary watchdog stay logon-bound on purpose: they
# start TrueFace and the gate counter, which need a real desktop.
_LOGON_FREE_INSTALLERS = {
    SYSTEM_WATCHDOG_TASK: _install_system_watchdog,
    NIGHTLY_TASK: _install_system_nightly,
}


def _enable(name: str) -> bool:
    """Switch a task back on, and believe only the task's own state after."""
    _run(["schtasks", "/change", "/tn", name, "/enable"])
    xml = _task_xml(name)
    enabled = _ENABLED.search(xml) if xml else None
    return bool(xml) and (enabled is None or enabled.group(1) == "true")


def _task_state(name: str) -> dict:
    xml = _task_xml(name)
    if not xml:
        return {"exists": False}
    logon = _LOGON_TYPE.search(xml)
    enabled = _ENABLED.search(xml)
    logon_type = (logon.group(1) if logon else "").lower()
    state = {
        "exists": True,
        "enabled": (enabled.group(1) == "true") if enabled else True,
        "needs_logon": bool(logon_type)
        and logon_type not in _RUNS_WITHOUT_LOGON,
    }
    state.update(_last_run(name))
    return state


def _never_ran(state: dict) -> bool:
    """Whether Task Scheduler says this task has not run even once.

    A task can exist, be enabled and need no logon and still never fire - the
    SYSTEM watchdog runs wscript in Windows' session 0, where a runner that
    the desktop accepts can fail silently. Until it has actually run, saying
    this PC recovers on its own is a guess, and a guess here costs a morning.
    """
    result = str(state.get("last_result", "")).strip()
    if result == _NEVER_RUN_RESULT:
        return True
    stamp = str(state.get("last_run", "")).strip()
    if not stamp or stamp.upper().startswith("N/A"):
        return True
    return bool(_NEVER_RUN_STAMP.search(stamp))


def _ran_well(state: dict) -> bool:
    if _never_ran(state):
        return False
    return str(state.get("last_result", "")).strip() in _RAN_WELL


def repair_tasks() -> dict:
    """Read every recovery task, repairing what can be repaired unattended.

    A task that is merely switched off is switched back on, and the two tasks
    that must not wait for a person - the logon-free watchdog and the nightly
    refresh - are registered again as SYSTEM when they are missing or bound to
    a logon.
    """
    report: dict[str, dict] = {}
    for name in TASKS:
        try:
            state = _task_state(name)
        except Exception as exc:
            logger.debug("PC RECOVERY: %s unreadable: %s", name, exc)
            state = {"exists": None, "error": "unreadable"}
        installer = _LOGON_FREE_INSTALLERS.get(name)
        needs_install = installer is not None and (
            state.get("exists") is False or state.get("needs_logon")
        )
        if needs_install:
            logger.warning(
                "PC RECOVERY: %s would wait for a logon; registering it as "
                "SYSTEM so a night-time failure cannot cost a morning of "
                "photos",
                name,
            )
            if installer():
                state = _task_state(name)
                state["repaired"] = True
            else:
                logger.warning(
                    "PC RECOVERY: could not register %s as SYSTEM; run "
                    "install_autostart.bat as administrator",
                    name,
                )
        if state.get("exists") and not state.get("enabled", True):
            logger.warning("PC RECOVERY: %s was disabled; re-enabling it", name)
            if _enable(name):
                state["enabled"] = True
                state["repaired"] = True
        report[name] = state
    return report


def pc_recovery_health() -> dict:
    """What the cloud needs to say whether this PC can recover on its own."""
    try:
        tasks = repair_tasks()
    except Exception as exc:
        logger.debug("PC RECOVERY: task read failed: %s", exc)
        tasks = {}
    missing = sorted(n for n, s in tasks.items() if s.get("exists") is False)
    unreadable = sorted(n for n, s in tasks.items() if s.get("exists") is None)
    off = sorted(
        n for n, s in tasks.items() if s.get("exists") and not s.get("enabled")
    )
    logon_bound = sorted(n for n, s in tasks.items() if s.get("needs_logon"))
    system_watchdog = tasks.get(SYSTEM_WATCHDOG_TASK, {})
    unattended = bool(
        system_watchdog.get("exists")
        and system_watchdog.get("enabled")
        and not system_watchdog.get("needs_logon")
    )
    proven = _ran_well(system_watchdog)
    nightly = tasks.get(NIGHTLY_TASK, {})
    nightly_unattended = bool(
        nightly.get("exists")
        and nightly.get("enabled")
        and not nightly.get("needs_logon")
    )
    health = {
        "boot_at_ist": boot_at_ist(),
        "read_at_ist": _ist(time.time()),
        "tasks": tasks,
        "tasks_missing": missing,
        "tasks_unreadable": unreadable,
        "tasks_disabled": off,
        "tasks_need_logon": logon_bound,
        # The one sentence that matters: after a reboot or a crash with nobody
        # logged on, only a task that needs no logon can put the campus agent
        # back, and without it a night-time failure costs the whole morning.
        "recovers_without_logon": unattended,
        # Registered is not the same as working. This says the logon-free
        # watchdog has actually run and ended well at least once, which is the
        # only evidence that a night-time failure would really be recovered.
        "logon_free_watchdog_proven": unattended and proven,
        "logon_free_watchdog_last_run": str(
            system_watchdog.get("last_run", "")
        ),
        "logon_free_watchdog_last_result": str(
            system_watchdog.get("last_result", "")
        ),
        # The 03:00 IST refresh is what puts the PC on merged code overnight.
        # Bound to a logon it is skipped on any night nobody logged in.
        "nightly_refresh_without_logon": nightly_unattended,
    }
    if not unattended:
        logger.warning(
            "PC RECOVERY: no logon-free watchdog on this PC (%s); a "
            "night-time reboot or crash would leave the campus without an "
            "agent until somebody logs in. Run install_autostart.bat as "
            "administrator.",
            SYSTEM_WATCHDOG_TASK,
        )
    if unattended and not proven:
        logger.warning(
            "PC RECOVERY: %s is registered and needs no logon but has not "
            "run yet (last result %s); unattended recovery is unproven until "
            "it does",
            SYSTEM_WATCHDOG_TASK,
            system_watchdog.get("last_result", "") or "none",
        )
    if not nightly_unattended:
        logger.warning(
            "PC RECOVERY: the 03:00 IST nightly refresh (%s) would not run "
            "with nobody logged on; the PC can then sit on stale code",
            NIGHTLY_TASK,
        )
    if logon_bound:
        logger.info(
            "PC RECOVERY: %s run only while somebody is logged on",
            ", ".join(logon_bound),
        )
    return health
