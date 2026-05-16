"""Completion recipe for make — completes Makefile target names."""

from __future__ import annotations

import os
import re

from ..commands import CommandRegistry
from ..completion import Completer, Completion, CompletionContext


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
    registry.register_external_completers("make", {0: MakeTargetCompleter()})
