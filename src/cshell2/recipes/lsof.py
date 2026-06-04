"""Completion recipe for lsof."""

from __future__ import annotations

import shutil

from ..commands import arg, flag_args, registry as command_registry
from ..completion import FileCompleter
from .ps import PidCompleter

_LSOF_OPTIONS: dict[str, str] = {
    "-a": "AND the selection criteria (default is OR)",
    "-b": "avoid kernel functions that may block",
    "-c": "list files for command names matching regex/string",
    "-d": "include / exclude files by file descriptor (e.g. 0,1,2)",
    "-D": "directory selection (a, b, i, l, r, R, u)",
    "-e": "tolerate errors on the named filesystem path",
    "-F": "produce output for parsing by other programs",
    "-g": "list files for processes whose group ID matches",
    "-h": "show help",
    "-i": "internet sockets (e.g. -i:80, -i tcp, -i @host)",
    "-K": "list tasks (threads) for owning processes",
    "-l": "show user IDs as numbers (don't look up names)",
    "-n": "no host name lookup",
    "-N": "list NFS files",
    "-o": "show file offset",
    "-O": "bypass overhead-warning prompt",
    "-p": "list files for the given process IDs",
    "-P": "no port name lookup",
    "-r": "repeat every N seconds until SIGINT",
    "-R": "list parent process IDs",
    "-s": "show file size",
    "-S": "set timeout for kernel functions (default 15s)",
    "-t": "terse output (PIDs only)",
    "-T": "TCP/UDP socket info (q, st, qs)",
    "-u": "list files for users (login or UID)",
    "-U": "list UNIX domain socket files",
    "-v": "show version info",
    "-w": "suppress warnings",
    "+c": "command-name column width (0 = unlimited)",
    "+d": "search files in directory (one level deep)",
    "+D": "recursively search files in directory",
    "+f": "format flags for fields",
    "+L": "list link counts",
}

_LSOF_ARGS: dict[str, object] = {
    "-c": "COMMAND",
    "-d": "FD-LIST",
    "-D": "MODE",
    "-g": "PGID",
    "-i": "[i46][proto][@host][:port]",
    "-p": ("PID-LIST", PidCompleter()),
    "-r": "SECONDS",
    "-S": "SECONDS",
    "-T": "MODE",
    "-u": "USER-LIST",
    "+c": "WIDTH",
    "+d": "DIR",
    "+D": "DIR",
}


def register() -> None:
    if shutil.which("lsof") is None:
        return
    command_registry.command(
        "lsof",
        help="list open files",
        params=[
            arg("path", nargs="*", help="file or directory", completer=FileCompleter()),
            *flag_args(_LSOF_OPTIONS, values=_LSOF_ARGS),
        ],
    )
