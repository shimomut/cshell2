"""Completion recipe for ps."""

from __future__ import annotations

import shutil
import subprocess

from ..commands import registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    OptionsCompleter,
)


# BSD-style flags share the macOS and Linux ps; we list a useful subset.
_PS_OPTIONS: dict[str, str] = {
    "-A": "show all processes",
    "-a": "show all processes with a controlling terminal except session leaders",
    "-d": "show all processes except session leaders",
    "-e": "show all processes (same as -A)",
    "-f": "full-format listing",
    "-l": "long format",
    "-j": "jobs format (sid, pgid)",
    "-u": "select by effective user",
    "-U": "select by real user",
    "-p": "select by process ID",
    "-G": "select by real group",
    "-g": "select by session or by group (BSD vs GNU)",
    "-t": "select by tty",
    "-x": "include processes with no controlling terminal (BSD)",
    "-w": "wide output (use -ww for unlimited)",
    "-o": "user-defined output format",
    "-c": "show command name only (no args)",
    "-H": "show process hierarchy",
    "-r": "restrict to running processes",
    "-T": "show all processes on this terminal",
    "-v": "virtual memory format",
    "-y": "do not show flags; show RSS instead of ADDR (with -l)",
}


class PidCompleter(Completer):
    """Completes running PIDs (with command name as description)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            result = subprocess.run(
                ["ps", "-e", "-o", "pid=,comm="],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        prefix = ctx.prefix
        out = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid, name = parts[0], parts[1].strip()
            if pid.startswith(prefix):
                out.append(Completion(value=pid, description=name))
        return out


_PS_ARGS: dict[str, object] = {
    "-u": "USER",
    "-U": "USER",
    "-p": ("PID", PidCompleter()),
    "-G": "GROUP",
    "-g": "PGID|SID",
    "-t": "TTY",
    "-o": "FORMAT",
}


def register() -> None:
    if shutil.which("ps") is None:
        return
    command_registry.command(
        "ps",
        help="report a snapshot of current processes",
        options_completer=OptionsCompleter(_PS_OPTIONS, args=_PS_ARGS),
    )
