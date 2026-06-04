"""Completion recipe for rm — platform-aware (BSD vs GNU)."""

from __future__ import annotations

import shutil
import sys

from ..commands import arg, flag_args, registry as command_registry
from ..completion import FileCompleter

# BSD rm (macOS).
_MACOS_OPTIONS: dict[str, str] = {
    "-d": "remove directories as well as other types of files",
    "-f": "force — ignore nonexistent files, never prompt",
    "-i": "interactive — prompt before each removal",
    "-I": "prompt once if removing 3+ files or recursively (with -R)",
    "-P": "overwrite regular files before deletion",
    "-R": "recursively remove directories and contents",
    "-r": "recursively remove directories and contents (same as -R)",
    "-v": "verbose — show files as they are removed",
    "-W": "attempt to undelete files (HFS / FFS only)",
    "-x": "stay on this filesystem when descending",
}

# GNU rm (Linux/coreutils).
_LINUX_OPTIONS: dict[str, str] = {
    "-d": "remove empty directories",
    "-f": "force — ignore nonexistent files, never prompt",
    "-i": "interactive — prompt before each removal",
    "-I": "prompt once if removing 3+ files or recursively",
    "-r": "recursively remove directories and contents",
    "-R": "recursively remove directories and contents (same as -r)",
    "-v": "verbose — show files as they are removed",
    "--interactive": "prompt according to WHEN (never, once, always)",
    "--no-preserve-root": "do not treat / specially",
    "--preserve-root": "do not remove / (default)",
    "--one-file-system": "stay on the source filesystem (with -r)",
    "--": "end of options — treat following args as filenames",
}

_LINUX_ARGS: dict[str, str] = {
    "--interactive": "WHEN",
}


def register() -> None:
    if shutil.which("rm") is None:
        return
    if sys.platform == "darwin":
        options, opt_args = _MACOS_OPTIONS, {}
    else:
        options, opt_args = _LINUX_OPTIONS, _LINUX_ARGS
    command_registry.command(
        "rm",
        help="remove files or directories",
        params=[
            arg("path", nargs="*", help="file or directory to remove", completer=FileCompleter()),
            *flag_args(options, values=opt_args),
        ],
    )
