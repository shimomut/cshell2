"""Completion recipe for cp — platform-aware (BSD vs GNU)."""

from __future__ import annotations

import shutil
import sys

from ..commands import arg, flag_args, registry as command_registry
from ..completion import FileCompleter

# BSD cp (macOS): subset of useful flags from `man cp` on Darwin.
_MACOS_OPTIONS: dict[str, str] = {
    "-a": "archive — preserve attrs, like -pPR",
    "-c": "use clonefile() if possible (copy-on-write)",
    "-f": "force overwrite without asking",
    "-H": "with -R, follow symlinks on the command line only",
    "-i": "interactive — prompt before overwriting",
    "-L": "with -R, follow all symlinks",
    "-n": "do not overwrite existing files",
    "-P": "do not follow symlinks (default with -R)",
    "-p": "preserve attributes — mode, ownership, timestamps",
    "-R": "recursive copy",
    "-r": "recursive copy (same as -R)",
    "-S": "do not attempt to make sparse holes",
    "-s": "create symbolic links instead of copying",
    "-v": "verbose — print files as they are copied",
    "-X": "do not copy extended attrs / resource forks",
}

# GNU cp (Linux/coreutils).
_LINUX_OPTIONS: dict[str, str] = {
    "-a": "archive — equivalent to -dR --preserve=all",
    "-b": "make backup of each existing destination file",
    "-d": "preserve symlinks (same as --no-dereference --preserve=links)",
    "-f": "force overwrite without asking",
    "-i": "interactive — prompt before overwriting",
    "-H": "follow command-line symlinks in source",
    "-l": "hard link files instead of copying",
    "-L": "always follow symlinks in source",
    "-n": "do not overwrite existing files",
    "-P": "never follow symlinks in source",
    "-p": "preserve attributes (mode, ownership, timestamps)",
    "-R": "recursive copy",
    "-r": "recursive copy (same as -R)",
    "-s": "create symbolic links instead of copying",
    "-T": "treat dst as a normal file (no /dst/src semantics)",
    "-t": "copy all sources into the given directory",
    "-u": "copy only when source is newer or destination is missing",
    "-v": "verbose — print files as they are copied",
    "-x": "stay on this filesystem",
    "--backup": "make a backup (with optional CONTROL)",
    "--preserve": "preserve the listed attributes",
    "--no-preserve": "don't preserve the listed attributes",
    "--reflink": "use copy-on-write when possible",
    "--sparse": "control sparse-file handling",
    "--strip-trailing-slashes": "strip trailing / from each source",
    "--update": "copy only when source newer or destination missing",
}

_LINUX_ARGS: dict[str, object] = {
    "-t": ("DIR", FileCompleter()),
    "--target-directory": ("DIR", FileCompleter()),
    "--preserve": "ATTR_LIST",
    "--no-preserve": "ATTR_LIST",
    "--reflink": "WHEN",
    "--sparse": "WHEN",
    "--backup": "CONTROL",
    "-S": "SUFFIX",
}


def register() -> None:
    if shutil.which("cp") is None:
        return
    if sys.platform == "darwin":
        options, opt_args = _MACOS_OPTIONS, {}
    else:
        options, opt_args = _LINUX_OPTIONS, _LINUX_ARGS
    command_registry.command(
        "cp",
        help="copy files and directories",
        params=[
            arg("path", nargs="*", help="source or destination path", completer=FileCompleter()),
            *flag_args(options, values=opt_args),
        ],
    )
