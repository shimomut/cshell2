"""Completion recipe for du."""

from __future__ import annotations

from ..commands import CommandRegistry
from ..completion import FileCompleter, OptionsCompleter

DU_OPTIONS: dict[str, str] = {
    "-0": "end each output line with NUL instead of newline",
    "-a": "write counts for all files, not just directories",
    "-c": "produce a grand total",
    "-d": "print the total for a directory only if it is N or fewer levels deep",
    "-h": "print sizes in human readable format (e.g. 1K 234M 2G)",
    "-H": "like -h but use powers of 1000 not 1024",
    "-k": "use 1024-byte blocks",
    "-l": "count sizes many times if hard linked",
    "-L": "dereference all symbolic links",
    "-m": "use 1048576-byte (1 MiB) blocks",
    "-P": "do not follow symbolic links (default)",
    "-s": "display only a total for each argument",
    "-S": "do not include size of subdirectories",
    "-t": "exclude entries smaller than SIZE (or, with -t -N, greater than N)",
    "-x": "skip directories on different file systems",
    "--apparent-size": "print apparent sizes rather than disk usage",
    "--exclude": "exclude files that match PATTERN",
    "--max-depth": "print the total for a directory only if N or fewer levels deep",
    "--time": "show the modification time of any file or directory",
}


DU_ARGS: dict[str, str] = {
    "-B": "SIZE",
    "-d": "N",
    "--max-depth": "N",
    "-t": "SIZE",
    "--threshold": "SIZE",
    "--exclude": "PATTERN",
    "--time-style": "STYLE",
}


def register(registry: CommandRegistry) -> None:
    registry.register_external_completers("du", {
        None: OptionsCompleter(DU_OPTIONS, args=DU_ARGS),
        0: FileCompleter(),
        1: FileCompleter(),
        2: FileCompleter(),
    })
