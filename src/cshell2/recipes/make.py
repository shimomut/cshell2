"""Completion recipe for make — completes Makefile target names and flags."""

from __future__ import annotations

import os
import re

from ..commands import CommandRegistry
from ..completion import Completer, Completion, CompletionContext, OptionsCompleter


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

MAKE_ARGS: dict[str, str] = {
    "-C": "DIR",
    "-f": "FILE",
    "-j": "N",
    "-l": "N",
    "-o": "FILE",
    "-W": "FILE",
}


class MakeTargetCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        targets = self._parse_targets()
        return [
            Completion(value=t)
            for t in targets
            if t.startswith(ctx.prefix)
        ]

    def _parse_targets(self) -> list[str]:
        makefile = self._find_makefile()
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

    def _find_makefile(self) -> str | None:
        for name in ("Makefile", "makefile", "GNUmakefile"):
            if os.path.isfile(name):
                return name
        return None


def register(registry: CommandRegistry) -> None:
    registry.register_external_completers("make", {
        None: OptionsCompleter(MAKE_OPTIONS, args=MAKE_ARGS),
        0: MakeTargetCompleter(),
        1: MakeTargetCompleter(),
        2: MakeTargetCompleter(),
    })
