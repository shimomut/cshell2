"""Completion recipe for make — completes Makefile target names and flags."""

from __future__ import annotations

import os
import re

from ..commands import arg, registry as command_registry
from ..completion import Completer, Completion, CompletionContext, DirCompleter, FileCompleter


def _find_dash_c_dir(args: list[str]) -> str | None:
    """Return the directory passed via -C in args, or None."""
    for i, tok in enumerate(args):
        if tok == "-C" and i + 1 < len(args):
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
    command_registry.command(
        "make",
        help="build targets from a Makefile",
        params=[
            arg("target", nargs="*", help="target to build",
                completer=MakeTargetCompleter()),
            arg("-B", action="store_true", help="unconditionally make all targets"),
            arg("-C", metavar="DIR", help="change to directory before doing anything",
                completer=DirCompleter()),
            arg("-d", action="store_true", help="print lots of debugging information"),
            arg("-e", action="store_true", help="give environment variables precedence over Makefile variables"),
            arg("-f", metavar="FILE", help="read FILE as the Makefile", completer=FileCompleter()),
            arg("-i", action="store_true", help="ignore errors from recipes"),
            arg("-j", metavar="N", help="number of parallel jobs (omit for unlimited)"),
            arg("-k", action="store_true", help="keep going after errors as much as possible"),
            arg("-l", metavar="N", help="don't start new jobs if load average is above N"),
            arg("-n", action="store_true", help="print commands without executing them (dry run)"),
            arg("-o", metavar="FILE", help="do not remake FILE even if it is older than its dependencies",
                completer=FileCompleter()),
            arg("-p", action="store_true", help="print make's internal database"),
            arg("-q", action="store_true", help="exit 0 if all targets are up to date, 1 otherwise"),
            arg("-r", action="store_true", help="disable built-in implicit rules"),
            arg("-R", action="store_true", help="disable built-in variable settings"),
            arg("-s", action="store_true", help="silent mode — do not echo recipes"),
            arg("-S", action="store_true", help="cancel the effect of -k"),
            arg("-t", action="store_true", help="touch targets instead of running their recipes"),
            arg("-v", action="store_true", help="print version information"),
            arg("-w", action="store_true", help="print working directory before and after processing"),
            arg("-W", metavar="FILE", help="pretend FILE was just modified", completer=FileCompleter()),
            arg("--warn-undefined-variables", action="store_true",
                help="warn when an undefined variable is referenced"),
        ],
    )
