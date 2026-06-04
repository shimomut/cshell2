"""Completion recipe for mv — platform-aware (BSD vs GNU)."""

from __future__ import annotations

import shutil
import sys

from ..commands import arg, flag_args, registry as command_registry
from ..completion import FileCompleter

# BSD mv (macOS).
_MACOS_OPTIONS: dict[str, str] = {
    "-f": "force overwrite without asking",
    "-i": "interactive — prompt before overwriting",
    "-n": "do not overwrite existing files",
    "-h": "if the target is a symlink to a directory, don't follow it",
    "-v": "verbose — print files as they are moved",
}

# GNU mv (Linux/coreutils).
_LINUX_OPTIONS: dict[str, str] = {
    "-b": "make a backup of each existing destination file",
    "-f": "force overwrite without asking",
    "-i": "interactive — prompt before overwriting",
    "-n": "do not overwrite existing files",
    "-T": "treat dst as a normal file (no /dst/src semantics)",
    "-t": "move all sources into the given directory",
    "-u": "move only when source is newer or destination is missing",
    "-v": "verbose — print files as they are moved",
    "--backup": "make a backup (with optional CONTROL)",
    "--strip-trailing-slashes": "strip trailing / from each source",
    "--update": "move only when source newer or destination missing",
}

_LINUX_ARGS: dict[str, object] = {
    "-t": ("DIR", FileCompleter()),
    "--target-directory": ("DIR", FileCompleter()),
    "--backup": "CONTROL",
    "-S": "SUFFIX",
}


def register() -> None:
    if shutil.which("mv") is None:
        return
    if sys.platform == "darwin":
        options, opt_args = _MACOS_OPTIONS, {}
    else:
        options, opt_args = _LINUX_OPTIONS, _LINUX_ARGS
    command_registry.command(
        "mv",
        help="move (rename) files",
        params=[
            arg("path", nargs="*", help="source or destination path", completer=FileCompleter()),
            *flag_args(options, values=opt_args),
        ],
    )
