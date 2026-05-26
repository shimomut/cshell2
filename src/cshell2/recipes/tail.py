"""Completion recipe for tail."""

from __future__ import annotations

from ..commands import registry
from ..completion import FileCompleter, OptionsCompleter

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
    registry.register_external_completers("tail", {
        None: OptionsCompleter(TAIL_OPTIONS, args=TAIL_ARGS),
        0: FileCompleter(),
        1: FileCompleter(),
        2: FileCompleter(),
    })
