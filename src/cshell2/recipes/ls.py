"""Completion recipe for ls — flag completion with multi-select TUI."""

from __future__ import annotations

from ..commands import arg, flag_args, registry as command_registry
from ..completion import FileCompleter

LS_OPTIONS: dict[str, str] = {
    "-a": "include entries starting with .",
    "-A": "do not list . and ..",
    "-c": "sort by ctime (time of last status change)",
    "-d": "list directories themselves, not their contents",
    "-F": "append indicator (*/=>@|) to entries",
    "-G": "colorize output (macOS) / suppress group column",
    "-g": "like -l but do not list owner",
    "-h": "print sizes like 1K, 234M, 2G",
    "-i": "print the index (inode) number of each file",
    "-l": "use a long listing format",
    "-L": "show info for symbolic link destinations",
    "-m": "fill width with a comma-separated list",
    "-n": "like -l but list numeric user and group IDs",
    "-o": "like -l but do not list group information",
    "-p": "append / indicator to directories",
    "-q": "print ? instead of non-graphic characters",
    "-r": "reverse order while sorting",
    "-R": "list subdirectories recursively",
    "-s": "print the allocated size of each file, in blocks",
    "-S": "sort by file size, largest first",
    "-t": "sort by modification time, newest first",
    "-u": "sort by last access time",
    "-U": "do not sort; list entries in directory order",
    "-v": "natural sort of version numbers within text",
    "-w": "set output width",
    "-x": "list entries by lines instead of by columns",
    "-X": "sort alphabetically by entry extension",
    "-1": "list one file per line",
}


def register() -> None:
    command_registry.command(
        "ls",
        help="list directory contents",
        params=[
            arg("path", nargs="*", help="file or directory", completer=FileCompleter()),
            *flag_args(LS_OPTIONS),
        ],
    )
