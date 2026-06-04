"""Completion recipe for kill and pkill."""

from __future__ import annotations

import subprocess

from ..commands import registry as command_registry
from ..completion import Completer, Completion, CompletionContext, OptionsCompleter

KILL_OPTIONS: dict[str, str] = {
    "-1": "SIGHUP — reload config / hangup",
    "-2": "SIGINT — interrupt (same as Ctrl+C)",
    "-3": "SIGQUIT — quit and produce core dump",
    "-9": "SIGKILL — force kill (cannot be caught or ignored)",
    "-15": "SIGTERM — graceful termination (default)",
    "-SIGHUP": "reload config / hangup",
    "-SIGINT": "interrupt (same as Ctrl+C)",
    "-SIGQUIT": "quit and produce core dump",
    "-SIGKILL": "force kill (cannot be caught or ignored)",
    "-SIGTERM": "graceful termination (default)",
    "-SIGUSR1": "user-defined signal 1",
    "-SIGUSR2": "user-defined signal 2",
    "-SIGSTOP": "pause process (cannot be caught or ignored)",
    "-SIGCONT": "resume paused process",
    "-l": "list all available signal names",
}


class ProcessCompleter(Completer):
    """Completes running process PIDs and names."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            result = subprocess.run(
                ["ps", "-e", "-o", "pid=,comm="],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []

        prefix = ctx.prefix
        completions = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid, name = parts[0], parts[1].strip()
            if pid.startswith(prefix):
                completions.append(Completion(value=pid, description=name))
        return completions


class ProcessNameCompleter(Completer):
    """Completes running process names (for pkill)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            result = subprocess.run(
                ["ps", "-e", "-o", "comm="],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []

        prefix = ctx.prefix
        seen: set[str] = set()
        completions = []
        for line in result.stdout.splitlines():
            name = line.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if name.startswith(prefix):
                completions.append(Completion(value=name))
        return sorted(completions, key=lambda c: c.value)


def register() -> None:
    command_registry.register_external_completers("kill", {
        None: OptionsCompleter(KILL_OPTIONS),
        0: ProcessCompleter(),
        1: ProcessCompleter(),
    }, description="send a signal to a process")
    command_registry.register_external_completers("pkill", {
        0: ProcessNameCompleter(),
    }, description="send a signal to processes by name")
