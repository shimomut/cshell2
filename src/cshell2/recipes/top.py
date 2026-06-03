"""Completion recipe for top — platform-aware (macOS vs Linux/procps)."""

from __future__ import annotations

import shutil
import sys

from ..commands import registry as command_registry
from ..completion import ChoiceCompleter, OptionsCompleter
from .ps import PidCompleter

# macOS top (from `top -h` on Darwin).
_MACOS_OPTIONS: dict[str, str] = {
    "-a": "set event counting mode to absolute",
    "-c": "set event counting mode (a, d, e, n)",
    "-d": "set event counting mode to delta",
    "-e": "set event counting mode to events",
    "-F": "calculate statistics for shared libraries (Frameworks)",
    "-f": "do not calculate framework statistics",
    "-h": "print usage and exit",
    "-i": "interval between framework updates (default 10)",
    "-l": "number of samples (0 = infinite, default 0 in screen mode)",
    "-ncols": "number of columns to display",
    "-o": "primary sort key",
    "-O": "secondary sort key",
    "-R": "do not traverse and report memory object map (default)",
    "-r": "traverse and report memory object map",
    "-S": "display swap and purgeable values in legend",
    "-s": "delay between samples in seconds",
    "-n": "maximum number of processes to display",
    "-stats": "comma-separated list of stats to display",
    "-pid": "show only the given process ID",
    "-user": "show only processes owned by the given user",
    "-U": "show only processes owned by the given user (alias)",
    "-u": "sort by CPU and show only running processes",
}

_MACOS_SORT_KEYS = [
    "pid", "command", "cpu", "cpu_me", "cpu_others", "csw", "time",
    "threads", "ports", "mregion", "mem", "rprvt", "purg", "vsize",
    "vprvt", "kprvt", "kshrd", "pgrp", "ppid", "state", "uid", "wq",
    "faults", "cow", "user", "msgsent", "msgrecv", "sysbsd", "sysmach",
    "pageins", "boosts", "instrs", "cycles",
]

_MACOS_ARGS: dict[str, object] = {
    "-c": ("MODE", ChoiceCompleter(["a", "d", "e", "n"])),
    "-i": "INTERVAL",
    "-l": "SAMPLES",
    "-ncols": "COLUMNS",
    "-o": ("KEY", ChoiceCompleter(_MACOS_SORT_KEYS)),
    "-O": ("SKEY", ChoiceCompleter(_MACOS_SORT_KEYS)),
    "-s": "SECONDS",
    "-n": "NPROCS",
    "-stats": "KEYS",
    "-pid": ("PID", PidCompleter()),
    "-user": "USER",
    "-U": "USER",
}


# Linux/procps top.
_LINUX_OPTIONS: dict[str, str] = {
    "-b": "batch mode — all output to stdout, no curses",
    "-c": "toggle command-line / program-name display",
    "-d": "delay between updates in seconds.tenths",
    "-E": "force summary memory scale (k, m, g, t, p, e)",
    "-e": "force task memory scale (k, m, g, t, p)",
    "-H": "show individual threads",
    "-h": "show help and exit",
    "-i": "toggle idle-process filter",
    "-n": "maximum number of iterations",
    "-O": "list available output fields and exit",
    "-o": "sort by the given field",
    "-p": "monitor only the given PIDs (comma-separated)",
    "-S": "toggle cumulative-time mode",
    "-s": "secure mode",
    "-U": "monitor processes owned by the given user (effective UID)",
    "-u": "monitor processes owned by the given user (real UID)",
    "-V": "show version and exit",
    "-w": "wide output (optional column count)",
}

_LINUX_ARGS: dict[str, object] = {
    "-d": "SECS.TENTHS",
    "-E": "SCALE",
    "-e": "SCALE",
    "-n": "ITERATIONS",
    "-o": "FIELD",
    "-p": ("PID[,PID]", PidCompleter()),
    "-U": "USER",
    "-u": "USER",
    "-w": "COLUMNS",
}


def register() -> None:
    if shutil.which("top") is None:
        return
    if sys.platform == "darwin":
        options, args = _MACOS_OPTIONS, _MACOS_ARGS
    else:
        options, args = _LINUX_OPTIONS, _LINUX_ARGS
    command_registry.register_external_completers("top", {
        None: OptionsCompleter(options, args=args),
    })
