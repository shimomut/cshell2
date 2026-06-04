"""Completion recipe for tail."""

from __future__ import annotations

from ..commands import arg, flag_args, registry as command_registry
from ..completion import FileCompleter

TAIL_OPTIONS: dict[str, str] = {
    "-c": "output the last N bytes",
    "-f": "follow the file as it grows",
    "-F": "like -f but retry if file becomes inaccessible",
    "-n": "output the last N lines (default: 10)",
    "-q": "suppress filename headers when multiple files given",
    "-r": "print lines in reverse order (macOS/BSD)",
    "-v": "always print filename headers",
}

TAIL_ARGS: dict[str, str] = {
    "-c": "N[bkm]",
    "-n": "N",
}


def register() -> None:
    command_registry.command(
        "tail",
        help="output the last part of files",
        params=[
            arg("file", nargs="*", help="file to tail", completer=FileCompleter()),
            *flag_args(TAIL_OPTIONS, values=TAIL_ARGS),
        ],
    )
