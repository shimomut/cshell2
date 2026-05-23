"""Completion recipe for make — completes Makefile target names and flags."""

from __future__ import annotations

import os
import re

from ..commands import registry
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


def _complete_dirs(ctx: CompletionContext) -> list[Completion]:
    """Return only directory completions (for -C <dir> argument)."""
    prefix = ctx.prefix
    if prefix:
        expanded = os.path.expanduser(prefix)
        directory = os.path.dirname(expanded) or "."
        partial = os.path.basename(expanded)
    else:
        directory = "."
        partial = ""
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    result = []
    for entry in sorted(entries):
        if entry.startswith(".") and not partial.startswith("."):
            continue
        if entry.lower().startswith(partial.lower()):
            full_path = os.path.join(directory, entry)
            if os.path.isdir(full_path):
                display = (
                    os.path.join(os.path.dirname(prefix), entry)
                    if prefix and os.path.dirname(prefix)
                    else entry
                )
                result.append(Completion(value=display + "/", display=entry + "/"))
    return result


def _find_dash_c_dir(args: list[str]) -> str | None:
    """Return the directory passed via -C in args, or None."""
    for i, arg in enumerate(args):
        if arg == "-C" and i + 1 < len(args):
            return args[i + 1]
    return None


class MakeTargetCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # If the immediately preceding arg is a flag that takes an argument,
        # delegate to the appropriate completer instead of showing targets.
        if ctx.args:
            prev = ctx.args[-1]
            hint = MAKE_ARGS.get(prev)
            if hint == "DIR":
                return _complete_dirs(ctx)
            elif hint == "FILE":
                return FileCompleter().complete(ctx)
            elif hint is not None:
                # Numeric argument (e.g. -j N, -l N) — no useful completions.
                return []

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
    # Register at enough positions to handle several flag+value pairs before
    # the target name (each pair uses 2 slots: flag + value).
    # Positions 0–7 covers up to 4 flag+value pairs.
    target_completer = MakeTargetCompleter()
    registry.register_external_completers("make", {
        None: OptionsCompleter(MAKE_OPTIONS, args=MAKE_ARGS),
        **{i: target_completer for i in range(8)},
    })
