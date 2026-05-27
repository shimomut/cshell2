"""Completion recipe for du — platform-aware (macOS vs Linux/GNU)."""

from __future__ import annotations

import sys

from ..commands import registry as command_registry
from ..completion import FileCompleter, OptionsCompleter

# macOS du  (BSD du — from `man du` on Darwin)
# usage: du [-Aclnx] [-H | -L | -P] [-g | -h | -k | -m]
#           [-a | -s | -d depth] [-B blocksize] [-I mask] [-t threshold] [file ...]
_MACOS_OPTIONS: dict[str, str] = {
    "-A": "display apparent sizes rather than disk usage",
    "-a": "write counts for all files, not just directories",
    "-c": "produce a grand total",
    "-d": "print the total for a directory only if it is N or fewer levels deep",
    "-g": "use 1-GiB (1073741824-byte) blocks",
    "-h": "print sizes in human-readable format (e.g. 1K 234M 2G)",
    "-H": "follow symbolic links on the command line only",
    "-I": "ignore files and directories matching mask",
    "-k": "use 1024-byte blocks (default)",
    "-l": "count sizes many times if hard linked",
    "-L": "follow all symbolic links",
    "-m": "use 1-MiB (1048576-byte) blocks",
    "-n": "ignore files and directories with the 'nodump' flag set",
    "-P": "do not follow symbolic links (default)",
    "-s": "display only a total for each argument",
    "-t": "exclude entries smaller than threshold (negative → exclude larger)",
    "-x": "skip directories on different file systems",
}

_MACOS_ARGS: dict[str, str] = {
    "-B": "blocksize",
    "-d": "N",
    "-I": "mask",
    "-t": "threshold",
}

# GNU/Linux du (coreutils)
_LINUX_OPTIONS: dict[str, str] = {
    "-0": "end each output line with NUL instead of newline",
    "-a": "write counts for all files, not just directories",
    "-b": "apparent bytes (equivalent to --apparent-size --block-size=1)",
    "-c": "produce a grand total",
    "-d": "print the total for a directory only if it is N or fewer levels deep",
    "-D": "dereference only symlinks listed on the command line",
    "-h": "print sizes in human-readable format (e.g. 1K 234M 2G)",
    "-H": "same as --si (use powers of 1000)",
    "-k": "use 1024-byte blocks",
    "-l": "count sizes many times if hard linked",
    "-L": "dereference all symbolic links",
    "-m": "use 1-MiB (1048576-byte) blocks",
    "-P": "do not follow symbolic links (default)",
    "-s": "display only a total for each argument",
    "-S": "do not include size of subdirectories in parent total",
    "-t": "exclude entries smaller than SIZE (negative → exclude larger)",
    "-x": "skip directories on different file systems",
    "--apparent-size": "print apparent sizes rather than disk usage",
    "--exclude": "exclude files that match PATTERN",
    "--max-depth": "print total only if N or fewer levels below the command-line argument",
    "--si": "use powers of 1000 (kB, MB, GB …)",
    "--time": "show the modification time of any file in the directory",
}

_LINUX_ARGS: dict[str, str] = {
    "-B": "SIZE",
    "--block-size": "SIZE",
    "-d": "N",
    "--max-depth": "N",
    "-t": "SIZE",
    "--threshold": "SIZE",
    "--exclude": "PATTERN",
    "--time-style": "STYLE",
}


def register() -> None:
    if sys.platform == "darwin":
        options, args = _MACOS_OPTIONS, _MACOS_ARGS
    else:
        options, args = _LINUX_OPTIONS, _LINUX_ARGS

    command_registry.register_external_completers("du", {
        None: OptionsCompleter(options, args=args),
        0: FileCompleter(),
        1: FileCompleter(),
        2: FileCompleter(),
    })
