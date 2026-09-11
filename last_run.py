"""How the previous campus agent process ended, read before this one logs.

A campus PC that dies and is restarted by the wrapper looks, from the cloud,
like an agent that simply went quiet: the cloud sees the gap and the new
process, never the reason. The reason is in the two log files on the PC, and
nobody is at the PC when it matters, so the new process reads the tail of
those files at startup and carries the finding to the cloud with its hello.

Nothing here is allowed to keep the agent from starting, and nothing that
could hold a secret or a path from the campus PC is sent.
"""

import re
from pathlib import Path

_HERE = Path(__file__).parent
AGENT_LOG = _HERE / "campus_agent.log"
WRAPPER_LOG = _HERE / "wrapper_campus.log"
# The wrapper caps its log at 500 lines and moves the rest here, which can
# happen between the exit it recorded and the process that reads it.
WRAPPER_LOG_OLD = _HERE / "wrapper_campus.log.old"
# Where this process found the agent log when it started, written before
# anything that can end the process, so a run that dies before it logs a
# single line still leaves a boundary behind it.
RUN_BOUNDARY = _HERE / "run_boundary.txt"

# Enough of the tail to hold a traceback and the lines around it, and small
# enough to read on a machine that is already busy starting up.
_TAIL_BYTES = 64 * 1024
_MAX_REASON_CHARS = 600

# The wrapper writes its own clock in front of the exit it saw, and that clock
# is the campus PC's, which runs on IST.
_EXIT_LINE = re.compile(
    r"^\[(?P<when>[^\]]{,40})\].*Agent stopped \(exit code: (?P<code>-?\d+)\)"
)
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
# A failure is a line the logger itself wrote at that level, not any line
# that happens to quote one: the startup warning quotes the previous error,
# and matching it anywhere would keep one error alive for ever.
_FAILURE_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)? \[(?:ERROR|CRITICAL)\]"
)
# A real traceback begins its own line. The startup warning that quotes one
# carries it after the log prefix, and must not be read as a new failure.
_TRACEBACK_LINE = re.compile(r"^\s*Traceback \(most recent call last\)")
# Written by the agent as its first line, so the tail can be cut to the run
# that died instead of reaching back into runs before it.
RUN_START_MARKER = "AGENT RUN START"
# Anything that looks like a path on the campus PC, so a public health page
# never carries the layout of that machine.
_PATH = re.compile(r"[A-Za-z]:\\[^\s\"']+|/(?:home|Users)/[^\s\"']+")
_SECRETISH = re.compile(
    r"(?i)(secret|password|passwd|pwd|token|api[_-]?key|key|auth|"
    r"authorization|bearer)\s*[=:]\s*\S+"
)
# A long run of token-shaped characters is a credential far more often than
# it is anything worth reading, so it goes too.
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_-]{24,}={0,2}\b")


def _tail(path: Path) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()


def _scrub(text: str) -> str:
    text = _PATH.sub("<path>", text)
    text = _SECRETISH.sub(r"\1=<hidden>", text)
    text = _LONG_TOKEN.sub("<hidden>", text)
    return text.strip()[:_MAX_REASON_CHARS]


def _boundary_offset() -> int | None:
    """The byte at which the run that just died began writing."""
    try:
        return int(RUN_BOUNDARY.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _lines_from(path: Path, offset: int) -> list[str] | None:
    """The log from that byte on, or None if the file has moved since."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if offset < 0 or offset > size:
                return None
            handle.seek(max(offset, size - _TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    return text.splitlines()


def mark_run_start() -> None:
    """Record where this run's own log begins, for whoever replaces it.

    This runs before the imports and checks that can end the process, since a
    run that exits before its first log line still has to be told apart from
    the run before it.
    """
    try:
        size = AGENT_LOG.stat().st_size if AGENT_LOG.exists() else 0
        RUN_BOUNDARY.write_text(str(size), encoding="utf-8")
    except OSError:
        pass


def _agent_lines() -> list[str]:
    """What the run that just died wrote, and nothing from before it."""
    offset = _boundary_offset()
    if offset is not None:
        lines = _lines_from(AGENT_LOG, offset)
        if lines is not None:
            return lines
    return _previous_run_lines(_tail(AGENT_LOG))


def _previous_run_lines(lines: list[str]) -> list[str]:
    """Only what the run that just died said.

    This process has not logged yet, so everything after the last start
    marker belongs to its predecessor.
    """
    for index in range(len(lines) - 1, -1, -1):
        if RUN_START_MARKER in lines[index]:
            return lines[index + 1:]
    return lines


def _last_failure(lines: list[str]) -> str:
    """The last thing the previous run said that could explain its end."""
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if _TRACEBACK_LINE.match(line) or _FAILURE_LINE.match(line):
            return _scrub(" | ".join(lines[index:index + 6]))
    return ""


def _wrapper_exit() -> tuple[str, str]:
    """When the wrapper saw the last process return, and with what code."""
    for path in (WRAPPER_LOG, WRAPPER_LOG_OLD):
        for line in reversed(_tail(path)):
            found = _EXIT_LINE.match(line)
            if found:
                return found.group("when").strip(), found.group("code")
    return "", ""


def previous_run_summary() -> dict:
    """When the last run stopped, how it exited and what it said last."""
    summary = {"ended_at": "", "exit_code": "", "last_error": ""}
    try:
        agent_lines = _agent_lines()
        summary["last_error"] = _last_failure(agent_lines)
        ended_at, exit_code = _wrapper_exit()
        summary["exit_code"] = exit_code
        if ended_at:
            # The wrapper's clock is the moment the process actually went,
            # which a silent crash never writes into the agent's own log.
            summary["ended_at"] = ended_at
        else:
            for line in reversed(agent_lines):
                found = _TIMESTAMP.match(line)
                if found:
                    summary["ended_at"] = found.group(1)
                    break
    except Exception:  # noqa: BLE001
        # Startup diagnostics must never be the thing that stops the agent.
        return summary
    return summary
