"""Completion recipe for chmod."""

from __future__ import annotations

import shutil

from ..commands import arg, flag_args, registry as command_registry
from ..completion import ChoiceCompleter, FileCompleter

CHMOD_OPTIONS: dict[str, str] = {
    "-f": "do not display diagnostic messages on failure",
    "-h": "change mode of symlink itself, not target",
    "-H": "with -R, follow symlinks on the command line only",
    "-L": "with -R, follow all symlinks",
    "-P": "with -R, do not follow any symlinks (default)",
    "-R": "recurse into directories",
    "-v": "verbose — show files as their mode is changed",
}

COMMON_MODES: list[str] = [
    "644",
    "664",
    "666",
    "700",
    "750",
    "755",
    "775",
    "777",
    "u+x",
    "u+w",
    "u+r",
    "u-x",
    "u-w",
    "g+x",
    "g+w",
    "g-w",
    "o+r",
    "o-r",
    "o-w",
    "a+x",
    "a+r",
    "a-w",
    "+x",
    "+w",
    "+r",
    "-x",
    "-w",
]


def register() -> None:
    if shutil.which("chmod") is None:
        return
    command_registry.command(
        "chmod",
        help="change file mode bits",
        params=[
            arg("mode", help="mode bits (e.g. 755 or u+x)", completer=ChoiceCompleter(COMMON_MODES)),
            arg("file", nargs="*", help="file or directory", completer=FileCompleter()),
            *flag_args(CHMOD_OPTIONS),
        ],
    )
