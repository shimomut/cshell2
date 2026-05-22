"""Completion recipe for df."""

from __future__ import annotations

from ..commands import CommandRegistry
from ..completion import FileCompleter, OptionsCompleter

DF_OPTIONS: dict[str, str] = {
    "-a": "include pseudo, duplicate, and inaccessible file systems",
    "-B": "scale sizes by SIZE before printing (e.g. -BM for megabytes)",
    "-h": "print sizes in human readable format (e.g. 1K 234M 2G)",
    "-H": "like -h but use powers of 1000 not 1024",
    "-i": "list inode information instead of block usage",
    "-k": "use 1024-byte blocks (default on many systems)",
    "-l": "limit listing to local file systems",
    "-m": "use 1048576-byte (1 MiB) blocks",
    "-P": "use POSIX output format",
    "-T": "print file system type",
    "-t": "limit listing to file systems of given type",
    "-x": "exclude file systems of the given type",
    "--total": "show a final grand total",
    "--output": "select output fields (source,fstype,size,used,avail,pcent,target)",
    "--sync": "invoke sync before getting usage info",
    "--no-sync": "do not invoke sync before getting usage info (default)",
}


def register(registry: CommandRegistry) -> None:
    registry.register_external_completers("df", {
        None: OptionsCompleter(DF_OPTIONS),
        0: FileCompleter(),
        1: FileCompleter(),
    })
