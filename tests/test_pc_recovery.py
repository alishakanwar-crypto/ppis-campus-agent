"""The campus PC must be able to restart its own agents without a person."""

import subprocess

import pc_recovery


def _xml(logon: str = "InteractiveToken", enabled: str = "true") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-16"?>'
        "<Task><Triggers><TimeTrigger><Enabled>true</Enabled>"
        "</TimeTrigger></Triggers>"
        f"<Principals><Principal><LogonType>{logon}</LogonType>"
        "</Principal></Principals>"
        f"<Settings><Enabled>{enabled}</Enabled></Settings></Task>"
    )


_LIST = (
    "Last Run Time: 15-09-2026 01:55:00\n"
    "Last Result: 0\n"
    "Next Run Time: 15-09-2026 07:20:00\n"
)

# What schtasks prints for a task that has never run once.
_NEVER_RUN_LIST = (
    "Last Run Time: 30-11-1999 00:00:00\n"
    "Last Result: 267011\n"
    "Next Run Time: 15-09-2026 10:45:00\n"
)


class _Schtasks:
    """Stands in for schtasks, answering per task name."""

    def __init__(
        self,
        xml_by_task: dict,
        enabled_calls: list | None = None,
        can_create: bool = True,
        list_by_task: dict | None = None,
    ):
        self.list_by_task = list_by_task or {}
        self.xml_by_task = xml_by_task
        self.enabled_calls = enabled_calls if enabled_calls is not None else []
        self.can_create = can_create
        self.created: list = []

    def __call__(self, args):
        name = args[args.index("/tn") + 1]
        if "/create" in args:
            self.created.append(name)
            if self.can_create:
                self.xml_by_task[name] = _xml(logon="ServiceAccount")
            return "SUCCESS" if self.can_create else ""
        if "/change" in args:
            self.enabled_calls.append(name)
            self.xml_by_task[name] = _xml(enabled="true")
            return "SUCCESS"
        xml = self.xml_by_task.get(name)
        if xml is None:
            return ""
        if "/xml" in args:
            return xml
        return self.list_by_task.get(name, _LIST)


def test_a_logon_bound_watchdog_is_registered_again_as_system(monkeypatch):
    fake = _Schtasks({name: _xml() for name in pc_recovery.TASKS})
    monkeypatch.setattr(pc_recovery, "_run", fake)
    monkeypatch.setattr(pc_recovery, "boot_at_ist", lambda: "15-09-2026 01:59:00 IST")

    health = pc_recovery.pc_recovery_health()

    assert fake.created == [
        pc_recovery.SYSTEM_WATCHDOG_TASK,
        pc_recovery.NIGHTLY_TASK,
    ]
    assert health["recovers_without_logon"] is True
    assert health["nightly_refresh_without_logon"] is True
    assert pc_recovery.SYSTEM_WATCHDOG_TASK not in health["tasks_need_logon"]
    assert health["boot_at_ist"] == "15-09-2026 01:59:00 IST"
    watchdog = health["tasks"][pc_recovery.WATCHDOG_TASK]
    assert watchdog["last_result"] == "0"
    assert watchdog["last_run"] == "15-09-2026 01:55:00"


def test_a_system_run_watchdog_keeps_photos_alive_without_a_logon(monkeypatch):
    tasks = {name: _xml() for name in pc_recovery.TASKS}
    tasks[pc_recovery.SYSTEM_WATCHDOG_TASK] = _xml(logon="ServiceAccount")
    monkeypatch.setattr(pc_recovery, "_run", _Schtasks(tasks))
    monkeypatch.setattr(pc_recovery, "boot_at_ist", lambda: "")

    health = pc_recovery.pc_recovery_health()

    # The two desktop-bound agents still need a logon; that is expected and
    # must not make the PC look unable to serve parents.
    assert pc_recovery.WATCHDOG_TASK in health["tasks_need_logon"]
    assert health["recovers_without_logon"] is True


def test_a_missing_system_watchdog_is_installed_by_the_agent(monkeypatch):
    present = {
        pc_recovery.BOOT_TASK: _xml(),
        pc_recovery.WATCHDOG_TASK: _xml(),
        pc_recovery.NIGHTLY_TASK: _xml(logon="ServiceAccount"),
    }
    fake = _Schtasks(present)
    monkeypatch.setattr(pc_recovery, "_run", fake)

    health = pc_recovery.pc_recovery_health()

    assert fake.created == [pc_recovery.SYSTEM_WATCHDOG_TASK]
    assert health["tasks_missing"] == []
    assert health["tasks"][pc_recovery.SYSTEM_WATCHDOG_TASK]["repaired"] is True
    assert health["recovers_without_logon"] is True


def test_a_watchdog_we_cannot_install_is_named(monkeypatch):
    present = {
        pc_recovery.BOOT_TASK: _xml(),
        pc_recovery.WATCHDOG_TASK: _xml(),
        pc_recovery.NIGHTLY_TASK: _xml(logon="ServiceAccount"),
    }
    monkeypatch.setattr(
        pc_recovery, "_run", _Schtasks(present, can_create=False)
    )

    health = pc_recovery.pc_recovery_health()

    assert health["tasks_missing"] == [pc_recovery.SYSTEM_WATCHDOG_TASK]
    assert health["recovers_without_logon"] is False


def test_a_logon_bound_watchdog_we_cannot_replace_is_not_promised(monkeypatch):
    tasks = {name: _xml() for name in pc_recovery.TASKS}
    monkeypatch.setattr(
        pc_recovery, "_run", _Schtasks(tasks, can_create=False)
    )

    health = pc_recovery.pc_recovery_health()

    assert pc_recovery.SYSTEM_WATCHDOG_TASK in health["tasks_need_logon"]
    assert health["recovers_without_logon"] is False


def test_a_logon_bound_nightly_refresh_is_registered_again_as_system(
    monkeypatch,
):
    # The 03:00 IST refresh is what puts the PC on merged code overnight; tied
    # to a logon it is skipped on any night nobody logged in.
    tasks = {name: _xml() for name in pc_recovery.TASKS}
    tasks[pc_recovery.SYSTEM_WATCHDOG_TASK] = _xml(logon="ServiceAccount")
    fake = _Schtasks(tasks)
    monkeypatch.setattr(pc_recovery, "_run", fake)

    health = pc_recovery.pc_recovery_health()

    assert fake.created == [pc_recovery.NIGHTLY_TASK]
    assert health["nightly_refresh_without_logon"] is True
    assert pc_recovery.NIGHTLY_TASK not in health["tasks_need_logon"]
    assert health["tasks"][pc_recovery.NIGHTLY_TASK]["repaired"] is True


def test_a_nightly_refresh_we_cannot_replace_is_not_promised(monkeypatch):
    tasks = {name: _xml() for name in pc_recovery.TASKS}
    tasks[pc_recovery.SYSTEM_WATCHDOG_TASK] = _xml(logon="ServiceAccount")
    monkeypatch.setattr(
        pc_recovery, "_run", _Schtasks(tasks, can_create=False)
    )

    health = pc_recovery.pc_recovery_health()

    assert health["nightly_refresh_without_logon"] is False
    assert pc_recovery.NIGHTLY_TASK in health["tasks_need_logon"]
    # The agent's own recovery is untouched by the nightly task's state.
    assert health["recovers_without_logon"] is True


def test_a_watchdog_that_has_never_run_is_not_called_proven(monkeypatch):
    # Registered, enabled, needs no logon - and has still never fired, which
    # is what a SYSTEM task whose runner cannot work in session 0 looks like.
    tasks = {name: _xml() for name in pc_recovery.TASKS}
    tasks[pc_recovery.SYSTEM_WATCHDOG_TASK] = _xml(logon="ServiceAccount")
    monkeypatch.setattr(
        pc_recovery,
        "_run",
        _Schtasks(
            tasks,
            list_by_task={
                pc_recovery.SYSTEM_WATCHDOG_TASK: _NEVER_RUN_LIST
            },
        ),
    )

    health = pc_recovery.pc_recovery_health()

    assert health["recovers_without_logon"] is True
    assert health["logon_free_watchdog_proven"] is False
    assert health["logon_free_watchdog_last_result"] == "267011"


def test_a_watchdog_that_has_run_well_is_proven(monkeypatch):
    tasks = {name: _xml() for name in pc_recovery.TASKS}
    tasks[pc_recovery.SYSTEM_WATCHDOG_TASK] = _xml(logon="ServiceAccount")
    monkeypatch.setattr(pc_recovery, "_run", _Schtasks(tasks))

    health = pc_recovery.pc_recovery_health()

    assert health["logon_free_watchdog_proven"] is True
    assert health["logon_free_watchdog_last_run"] == "15-09-2026 01:55:00"
    assert health["read_at_ist"].endswith("IST")


def test_a_watchdog_whose_run_failed_is_not_proven(monkeypatch):
    tasks = {name: _xml() for name in pc_recovery.TASKS}
    tasks[pc_recovery.SYSTEM_WATCHDOG_TASK] = _xml(logon="ServiceAccount")
    failed = (
        "Last Run Time: 15-09-2026 02:05:00\n"
        "Last Result: 1\n"
        "Next Run Time: 15-09-2026 02:10:00\n"
    )
    monkeypatch.setattr(
        pc_recovery,
        "_run",
        _Schtasks(
            tasks, list_by_task={pc_recovery.SYSTEM_WATCHDOG_TASK: failed}
        ),
    )

    health = pc_recovery.pc_recovery_health()

    assert health["logon_free_watchdog_proven"] is False
    assert health["logon_free_watchdog_last_result"] == "1"


def test_a_run_still_going_counts_as_the_watchdog_working(monkeypatch):
    tasks = {name: _xml() for name in pc_recovery.TASKS}
    tasks[pc_recovery.SYSTEM_WATCHDOG_TASK] = _xml(logon="ServiceAccount")
    running = (
        "Last Run Time: 15-09-2026 02:05:00\n"
        "Last Result: 267009\n"
        "Next Run Time: 15-09-2026 02:10:00\n"
    )
    monkeypatch.setattr(
        pc_recovery,
        "_run",
        _Schtasks(
            tasks, list_by_task={pc_recovery.SYSTEM_WATCHDOG_TASK: running}
        ),
    )

    assert pc_recovery.pc_recovery_health()[
        "logon_free_watchdog_proven"
    ] is True


def test_a_disabled_task_is_switched_back_on(monkeypatch):
    enabled_calls: list = []
    fake = _Schtasks(
        {
            name: _xml(logon="ServiceAccount", enabled="false")
            for name in pc_recovery.TASKS
        },
        enabled_calls,
    )
    monkeypatch.setattr(pc_recovery, "_run", fake)

    health = pc_recovery.pc_recovery_health()

    assert enabled_calls == list(pc_recovery.TASKS)
    assert health["tasks_disabled"] == []
    task = health["tasks"][pc_recovery.SYSTEM_WATCHDOG_TASK]
    assert task["repaired"] is True
    assert health["recovers_without_logon"] is True


def test_an_unreadable_task_list_never_raises(monkeypatch):
    def explode(args):
        raise OSError("schtasks is not answering")

    monkeypatch.setattr(pc_recovery, "_run", explode)

    health = pc_recovery.pc_recovery_health()

    assert health["recovers_without_logon"] is False
    unread = health["tasks"][pc_recovery.SYSTEM_WATCHDOG_TASK]
    assert unread["error"] == "unreadable"
    assert pc_recovery.SYSTEM_WATCHDOG_TASK in health["tasks_unreadable"]


class _Completed:
    def __init__(self, returncode: int, stdout: bytes):
        self.returncode = returncode
        self.stdout = stdout


def test_a_query_that_hangs_is_abandoned_not_waited_on(monkeypatch):
    # The agent is starting and parents are waiting behind nothing: a task
    # query that never answers must cost the startup nothing at all.
    seen: dict = {}

    def hang(args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)

    assert pc_recovery._run(["schtasks", "/query"]) == ""
    assert seen["timeout"] == pc_recovery._QUERY_TIMEOUT_SECONDS
    assert seen["capture_output"] is True


def test_a_refused_query_reads_as_no_answer(monkeypatch):
    # schtasks exits nonzero for a task that is not there and prints the
    # refusal on stdout; reading that as XML would invent a task.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: _Completed(1, b"ERROR: The system cannot find the..."),
    )

    assert pc_recovery._run(["schtasks", "/query"]) == ""


def test_a_plain_answer_is_read_as_written(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kw: _Completed(0, b"out")
    )

    assert pc_recovery._run(["schtasks", "/query"]) == "out"


def test_task_xml_in_utf_16_is_read_as_utf_16(monkeypatch):
    # schtasks /xml answers in UTF-16. Read as a byte codepage it keeps a NUL
    # between every letter, <LogonType> is never matched, and a logon-bound
    # watchdog would be reported as one that needs no logon.
    xml = _xml(logon="InteractiveToken").encode("utf-16")
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kw: _Completed(0, xml)
    )

    state = pc_recovery._task_state("PPIS Campus Agent")

    assert state["exists"] is True
    assert state["needs_logon"] is True
    assert state["enabled"] is True


def test_a_console_page_answer_is_still_read(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: _Completed(0, "Last Result: 0\n".encode("cp1252")),
    )

    assert "Last Result" in pc_recovery._run(["schtasks", "/query"])


def test_the_boot_time_is_reported_in_ist(monkeypatch):
    # 15-09-2026 01:57:12 IST, the reboot that cost a morning of photos.
    monkeypatch.setattr(pc_recovery.psutil, "boot_time", lambda: 1789417632.0)

    assert pc_recovery.boot_at_ist() == "15-09-2026 01:57:12 IST"


def test_an_unreadable_boot_time_is_left_blank(monkeypatch):
    def explode():
        raise OSError("no boot time here")

    monkeypatch.setattr(pc_recovery.psutil, "boot_time", explode)

    assert pc_recovery.boot_at_ist() == ""
