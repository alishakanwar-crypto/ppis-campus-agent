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


class _Schtasks:
    """Stands in for schtasks, answering per task name."""

    def __init__(
        self,
        xml_by_task: dict,
        enabled_calls: list | None = None,
        can_create: bool = True,
    ):
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
        return _LIST


def test_a_logon_bound_watchdog_is_registered_again_as_system(monkeypatch):
    fake = _Schtasks({name: _xml() for name in pc_recovery.TASKS})
    monkeypatch.setattr(pc_recovery, "_run", fake)
    monkeypatch.setattr(pc_recovery, "boot_at_ist", lambda: "15-09-2026 01:59:00 IST")

    health = pc_recovery.pc_recovery_health()

    assert fake.created == [pc_recovery.SYSTEM_WATCHDOG_TASK]
    assert health["recovers_without_logon"] is True
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
    def __init__(self, returncode: int, stdout: str):
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
        lambda args, **kw: _Completed(1, "ERROR: The system cannot find the..."),
    )

    assert pc_recovery._run(["schtasks", "/query"]) == ""


def test_the_query_runs_without_flashing_a_console(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kw: _Completed(0, "out")
    )

    assert pc_recovery._run(["schtasks", "/query"]) == "out"


def test_the_boot_time_is_reported_in_ist(monkeypatch):
    # 15-09-2026 01:57:12 IST, the reboot that cost a morning of photos.
    monkeypatch.setattr(pc_recovery.psutil, "boot_time", lambda: 1789417632.0)

    assert pc_recovery.boot_at_ist() == "15-09-2026 01:57:12 IST"


def test_an_unreadable_boot_time_is_left_blank(monkeypatch):
    def explode():
        raise OSError("no boot time here")

    monkeypatch.setattr(pc_recovery.psutil, "boot_time", explode)

    assert pc_recovery.boot_at_ist() == ""
