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

import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

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


def _ist(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, IST).strftime("%d-%m-%Y %H:%M:%S IST")


def boot_at_ist() -> str:
    """When Windows last started, so a night-time reboot can be seen."""
    try:
        return _ist(psutil.boot_time())
    except Exception as exc:
        logger.debug("PC RECOVERY: boot time unreadable: %s", exc)
        return ""


def _run(args: list[str]) -> str:
    try:
        done = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:
        logger.debug("PC RECOVERY: %s failed: %s", args[0], exc)
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout or ""


def _task_xml(name: str) -> str:
    # schtasks writes the XML as UTF-16 text; python decodes it for us, but a
    # missing task is an error exit and comes back as an empty string.
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


def repair_tasks() -> dict:
    """Read every recovery task, repairing what can be repaired unattended.

    A task that is merely switched off is switched back on, and a logon-free
    watchdog that is missing or bound to a logon is registered again as SYSTEM.
    """
    report: dict[str, dict] = {}
    for name in TASKS:
        try:
            state = _task_state(name)
        except Exception as exc:
            logger.debug("PC RECOVERY: %s unreadable: %s", name, exc)
            state = {"exists": None, "error": "unreadable"}
        needs_install = name == SYSTEM_WATCHDOG_TASK and (
            state.get("exists") is False or state.get("needs_logon")
        )
        if needs_install:
            logger.warning(
                "PC RECOVERY: no logon-free watchdog; installing it so a "
                "night-time failure cannot cost a morning of photos"
            )
            if _install_system_watchdog():
                state = _task_state(name)
                state["repaired"] = True
            else:
                logger.warning(
                    "PC RECOVERY: could not register %s; run "
                    "install_autostart.bat as administrator",
                    SYSTEM_WATCHDOG_TASK,
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
    health = {
        "boot_at_ist": boot_at_ist(),
        "tasks": tasks,
        "tasks_missing": missing,
        "tasks_unreadable": unreadable,
        "tasks_disabled": off,
        "tasks_need_logon": logon_bound,
        # The one sentence that matters: after a reboot or a crash with nobody
        # logged on, only a task that needs no logon can put the campus agent
        # back, and without it a night-time failure costs the whole morning.
        "recovers_without_logon": unattended,
    }
    if not unattended:
        logger.warning(
            "PC RECOVERY: no logon-free watchdog on this PC (%s); a "
            "night-time reboot or crash would leave the campus without an "
            "agent until somebody logs in. Run install_autostart.bat as "
            "administrator.",
            SYSTEM_WATCHDOG_TASK,
        )
    if logon_bound:
        logger.info(
            "PC RECOVERY: %s run only while somebody is logged on",
            ", ".join(logon_bound),
        )
    return health
