"""Completion recipe for make — completes Makefile target names and flags."""

from __future__ import annotations

import os
import re

from ..commands import registry as command_registry
from ..completion import Completer, Completion, CompletionContext, DirCompleter, FileCompleter, OptionsCompleter


MAKE_OPTIONS: dict[str, str] = {
    "-B": "unconditionally make all targets",
    "-C": "change to directory before doing anything",
    "-d": "print lots of debugging information",
    "-e": "give environment variables precedence over Makefile variables",
    "-f": "read FILE as the Makefile",
    "-i": "ignore errors from recipes",
    "-j": "number of parallel jobs (omit for unlimited)",
    "-k": "keep going after errors as much as possible",
    "-l": "don't start new jobs if load average is above N",
    "-n": "print commands without executing them (dry run)",
    "-o": "do not remake FILE even if it is older than its dependencies",
    "-p": "print make's internal database",
    "-q": "exit 0 if all targets are up to date, 1 otherwise",
    "-r": "disable built-in implicit rules",
    "-R": "disable built-in variable settings",
    "-s": "silent mode — do not echo recipes",
    "-S": "cancel the effect of -k",
    "-t": "touch targets instead of running their recipes",
    "-v": "print version information",
    "-w": "print working directory before and after processing",
    "-W": "pretend FILE was just modified",
    "--warn-undefined-variables": "warn when an undefined variable is referenced",
}

MAKE_ARGS = {
    "-C": ("DIR", DirCompleter()),
    "-f": ("FILE", FileCompleter()),
    "-j": "N",
    "-l": "N",
    "-o": ("FILE", FileCompleter()),
    "-W": ("FILE", FileCompleter()),
}


def _find_dash_c_dir(args: list[str]) -> str | None:
    """Return the directory passed via -C in args, or None."""
    for i, arg in enumerate(args):
        if arg == "-C" and i + 1 < len(args):
            return args[i + 1]
    return None


class MakeTargetCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # Find -C <dir> anywhere in the preceding args so we look in the right place.
        makefile_dir = _find_dash_c_dir(ctx.args)
        targets = self._parse_targets(makefile_dir)
        return [
            Completion(value=t)
            for t in targets
            if t.startswith(ctx.prefix)
        ]

    def _parse_targets(self, directory: str | None = None) -> list[str]:
        makefile = self._find_makefile(directory)
        if not makefile:
            return []
        try:
            with open(makefile) as f:
                content = f.read()
        except OSError:
            return []
        targets = []
        for line in content.splitlines():
            m = re.match(r'^([a-zA-Z0-9_][a-zA-Z0-9_./-]*)\s*:(?!=)', line)
            if m:
                targets.append(m.group(1))
        return sorted(set(targets))

    def _find_makefile(self, directory: str | None = None) -> str | None:
        base = directory or "."
        for name in ("Makefile", "makefile", "GNUmakefile"):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return path
        return None


def register() -> None:
    # _positional_index() in shell.py strips flags and their values before
    # looking up the positional completer, so positions 0–2 here refer to
    # the 1st, 2nd, and 3rd *actual* targets — flags never inflate the index.
    target_completer = MakeTargetCompleter()
    command_registry.register_external_completers("make", {
        None: OptionsCompleter(MAKE_OPTIONS, args=MAKE_ARGS),
        **{i: target_completer for i in range(3)},
    })
