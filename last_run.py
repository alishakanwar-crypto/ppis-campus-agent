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

# Enough of the tail to hold a traceback and the lines around it, and small
# enough to read on a machine that is already busy starting up.
_TAIL_BYTES = 64 * 1024
_MAX_REASON_CHARS = 600

_EXIT_CODE = re.compile(r"Agent stopped \(exit code: (-?\d+)\)")
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
# Anything that looks like a path on the campus PC, so a public health page
# never carries the layout of that machine.
_PATH = re.compile(r"[A-Za-z]:\\[^\s\"']+|/(?:home|Users)/[^\s\"']+")
_SECRETISH = re.compile(r"(?i)(secret|password|token)\s*[=:]\s*\S+")


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
    return text.strip()[:_MAX_REASON_CHARS]


def _last_failure(lines: list[str]) -> str:
    """The last thing the previous run said that could explain its end."""
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if (
            "Traceback (most recent call last)" in line
            or "[CRITICAL]" in line
            or "[ERROR]" in line
        ):
            return _scrub(" | ".join(lines[index:index + 6]))
    return ""


def previous_run_summary() -> dict:
    """When the last run stopped, how it exited and what it said last."""
    summary = {"ended_at": "", "exit_code": "", "last_error": ""}
    try:
        agent_lines = _tail(AGENT_LOG)
        for line in reversed(agent_lines):
            found = _TIMESTAMP.match(line)
            if found:
                summary["ended_at"] = found.group(1)
                break
        summary["last_error"] = _last_failure(agent_lines)
        for line in reversed(_tail(WRAPPER_LOG)):
            found = _EXIT_CODE.search(line)
            if found:
                summary["exit_code"] = found.group(1)
                break
    except Exception:  # noqa: BLE001
        # Startup diagnostics must never be the thing that stops the agent.
        return summary
    return summary
