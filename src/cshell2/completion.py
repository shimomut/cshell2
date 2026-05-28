"""Completion engine — Completer ABC, CompletionContext, built-in completers."""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .context import Context


@dataclass
class CompletionContext:
    command: str | None
    args: list[str]
    arg_index: int
    prefix: str
    line: str
    shell_context: Context | None = None


@dataclass
class Completion:
    value: str
    display: str = ""
    description: str = ""
    multi_select: bool = False
    combinable: bool = False  # True for single-char flags that can be merged (-a -l → -al)
    arg_hint: str = ""        # non-empty when the flag requires a following argument (e.g. "N")
    is_arg_hint: bool = False  # True when this completion IS the hint for a preceding flag's value

    def __post_init__(self):
        if not self.display:
            self.display = self.value


class Completer(ABC):
    @abstractmethod
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        ...

    def should_activate(self, ctx: CompletionContext) -> bool:
        return True


class DirCompleter(Completer):
    """Completes directory paths only (no files)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
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
                    display_path = (
                        os.path.join(os.path.dirname(prefix), entry)
                        if prefix and os.path.dirname(prefix)
                        else entry
                    )
                    result.append(Completion(value=display_path + "/", display=entry + "/"))
        return result


class FileCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if prefix:
            expanded_prefix = os.path.expanduser(prefix)
            directory = os.path.dirname(expanded_prefix) or "."
            partial = os.path.basename(expanded_prefix)
        else:
            directory = "."
            partial = ""

        try:
            entries = os.listdir(directory)
        except OSError:
            return []

        dirs = []
        files = []
        for entry in sorted(entries):
            if entry.startswith(".") and not partial.startswith("."):
                continue
            if entry.lower().startswith(partial.lower()):
                full_path = os.path.join(directory, entry)
                display_path = os.path.join(os.path.dirname(prefix), entry) if prefix and os.path.dirname(prefix) else entry
                if os.path.isdir(full_path):
                    dirs.append(Completion(value=display_path + "/", display=entry + "/"))
                else:
                    files.append(Completion(value=display_path, display=entry))
        return dirs + files


class CommandNameCompleter(Completer):
    def __init__(self, registry):
        self._registry = registry

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        results = []

        for name in sorted(self._registry.list_commands()):
            if name.startswith(prefix):
                results.append(Completion(value=name, description="command"))

        if hasattr(self._registry, "list_aliases"):
            for name, expansion in sorted(self._registry.list_aliases().items()):
                if name.startswith(prefix):
                    results.append(Completion(
                        value=name, description=f"alias → {expansion}"
                    ))

        for cmd in self._find_system_commands(prefix):
            results.append(Completion(value=cmd, description="system"))

        return results

    def _find_system_commands(self, prefix: str) -> list[str]:
        if not prefix:
            return []
        seen = set()
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        for d in path_dirs:
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for entry in entries:
                if entry.startswith(prefix) and entry not in seen:
                    full = os.path.join(d, entry)
                    if os.access(full, os.X_OK):
                        seen.add(entry)
        return sorted(seen)


class ChoiceCompleter(Completer):
    def __init__(self, choices: list[str]):
        self.choices = choices

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=c)
            for c in self.choices
            if c.startswith(ctx.prefix)
        ]


class CallbackCompleter(Completer):
    """Completer that calls a function to get the current list of choices."""

    def __init__(self, func):
        self.func = func

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=c)
            for c in self.func()
            if c.startswith(ctx.prefix)
        ]


class OptionsCompleter(Completer):
    """Completer for command-line flags with multi-select TUI support.

    Register under the ``None`` key in a completers dict to activate at any
    argument position when the user types a ``-``-prefixed token:

        registry.register_external_completers("ls", {
            None: OptionsCompleter({"-l": "long format", "-a": "show hidden"}),
            0: FileCompleter(),
        })
    """

    def __init__(
        self,
        options: dict[str, str],
        args: dict[str, str | tuple[str, Completer]] | None = None,
    ):
        self.options = options
        # args values may be a plain hint string ("N") or a (hint, value_completer)
        # tuple when a specific completer should be used for that flag's value.
        self.args: dict[str, str] = {}
        self._value_completers: dict[str, Completer] = {}
        for flag, spec in (args or {}).items():
            if isinstance(spec, tuple):
                hint, vc = spec
                self.args[flag] = hint
                self._value_completers[flag] = vc
            else:
                self.args[flag] = spec

    def should_activate(self, ctx: CompletionContext) -> bool:
        return ctx.prefix.startswith("-")

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        used = self._used_flags(ctx)
        result = []
        # Iterate the union of options and args so that value-taking flags
        # registered only in `args` (without a description in `options`) are
        # still shown as completions.
        all_flags = sorted(set(self.options) | set(self.args))
        for flag in all_flags:
            if not flag.startswith(prefix):
                continue
            if flag in used:
                continue
            desc = self.options.get(flag, "")
            arg_hint = self.args.get(flag, "")
            result.append(Completion(
                value=flag,
                description=desc,
                multi_select=True,
                combinable=(len(flag) == 2 and flag.startswith("-") and not arg_hint),
                arg_hint=arg_hint,
            ))
        return result

    def get_preceding_flag_hint(
        self, ctx: CompletionContext
    ) -> tuple[str, str, str, Completer | None] | None:
        """Return (flag, hint, description, value_completer) if the last completed arg is a value-taking flag.

        ``value_completer`` is a :class:`Completer` when the flag has a registered
        value completer (e.g. ``"-C": ("DIR", DirCompleter())``), otherwise ``None``.
        Returns ``None`` entirely when the preceding arg is not a known value-taking flag.
        """
        if not ctx.args:
            return None
        last_arg = ctx.args[-1]
        if not last_arg.startswith("-"):
            return None
        hint = self.args.get(last_arg)
        if not hint:
            return None
        description = self.options.get(last_arg, "")
        value_completer = self._value_completers.get(last_arg)
        return (last_arg, hint, description, value_completer)

    def _used_flags(self, ctx: CompletionContext) -> set[str]:
        """Return the set of option flags already present in ctx.args."""
        used: set[str] = set()
        for arg in ctx.args:
            if not arg.startswith("-"):
                continue
            if arg.startswith("--"):
                used.add(arg)
            else:
                # Split short-flag clusters: -hs → {-h, -s}
                for ch in arg[1:]:
                    used.add(f"-{ch}")
        return used


class ConditionalCompleter(Completer):
    """Picks a sub-completer based on preceding args."""

    def __init__(self, mapping: dict[tuple, Completer]):
        self.mapping = mapping

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        key = tuple(ctx.args)
        completer = self.mapping.get(key)
        if completer:
            return completer.complete(ctx)
        for length in range(len(ctx.args), 0, -1):
            partial_key = tuple(ctx.args[:length])
            if partial_key in self.mapping:
                return self.mapping[partial_key].complete(ctx)
        return []


# ---------------------------------------------------------------------------
# Cobra-protocol fallback
# ---------------------------------------------------------------------------
#
# Most modern Go CLIs (kubectl, helm, gh, argocd, k9s, doctl, linkerd, …) are
# built on the spf13/cobra framework, which exposes a hidden ``__complete``
# subcommand.  When a tool registers shell completions, cobra inserts a
# function that re-invokes the tool itself like::
#
#     $ kubectl __complete get po ""
#     pod         retrieve a list of pods
#     pods        (alias)
#     poddisruptionbudget
#     poddisruptionbudgets
#     :4          ← directive byte (4 = nospace, 2 = nofiles, …)
#
# Lines before the trailing ``:N`` are candidates; each line is
# ``name\tdescription`` (description optional).  This module drives that
# protocol directly — no bash, no bash-completion script needed.


# Sentinel returned by the probe to indicate "not a cobra command".
_NOT_COBRA = object()


class CobraCompleter(Completer):
    """Fallback completer that calls a tool's hidden ``__complete`` subcommand.

    Cobra-based CLIs (kubectl, helm, gh, argocd, k9s, doctl, …) ship a
    completion function that's just a wrapper around ``<cmd> __complete``.
    Calling that subcommand directly skips bash entirely, returns richer
    data (descriptions per candidate), and works on any host that has the
    tool itself installed.

    Per-command detection: on first encounter of a command, we run
    ``<cmd> __complete --help`` once and check whether the response looks
    like a cobra completion handler.  Result is cached for the rest of the
    shell session.
    """

    def __init__(self, *, timeout: float = 1.5) -> None:
        self._timeout = timeout
        # Per-command probe cache: command name → bool.
        # Missing entry means "not yet probed".
        self._is_cobra: dict[str, bool] = {}
        # Per-line completion cache: line → list[(value, description)].
        self._results: dict[str, list[tuple[str, str]]] = {}

    def should_activate(self, ctx: CompletionContext) -> bool:
        if not ctx.command:
            return False
        # Only activate for commands resolvable on PATH — avoids spawning a
        # subprocess for typos / unknown words.
        if shutil.which(ctx.command) is None:
            return False
        return self._is_cobra_command(ctx.command)

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.command or not self._is_cobra_command(ctx.command):
            return []
        line = ctx.line
        if line in self._results:
            results = self._results[line]
        else:
            results = self._invoke(ctx.command, ctx.args, ctx.prefix)
            self._results[line] = results
        prefix = ctx.prefix
        return [
            Completion(value=v, description=d)
            for v, d in results
            if v.startswith(prefix)
        ]

    # ── detection ────────────────────────────────────────────────────────

    def _is_cobra_command(self, command: str) -> bool:
        """Return True if *command* responds to ``__complete --help`` like cobra.

        Probes once per command per shell session; result is cached.
        """
        if command in self._is_cobra:
            return self._is_cobra[command]
        result = self._probe(command)
        self._is_cobra[command] = result
        return result

    def _probe(self, command: str) -> bool:
        """One-shot probe: does *command* speak the cobra protocol?"""
        try:
            proc = subprocess.run(
                [command, "__complete", "--help"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        # Cobra's __complete help text contains a recognizable phrase.  Both
        # stdout and stderr are checked because cobra writes to stdout but
        # other tools may surface our probe via stderr.
        blob = (proc.stdout or "") + (proc.stderr or "")
        if "shell completion" in blob.lower() or "ShellCompDirective" in blob:
            return True
        # Heuristic fallback: cobra always exits 0 on `__complete --help` and
        # mentions "__complete" itself in the usage line.  Many non-cobra
        # tools either error out or emit completely unrelated help text.
        if proc.returncode == 0 and "__complete" in blob:
            return True
        return False

    # ── invocation ───────────────────────────────────────────────────────

    def _invoke(
        self, command: str, args: list[str], prefix: str
    ) -> list[tuple[str, str]]:
        """Run ``<cmd> __complete <args> <prefix>``; return [(value, desc), …]."""
        argv = [command, "__complete", *args, prefix]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        # Cobra exits 0 on success; some tools may return non-zero when no
        # candidates apply.  Treat non-zero as empty.
        if proc.returncode != 0:
            return []
        return _parse_cobra_output(proc.stdout)


def _parse_cobra_output(stdout: str) -> list[tuple[str, str]]:
    """Parse cobra ``__complete`` stdout into (value, description) pairs.

    Format::

        name\tdescription
        name              (description optional)
        :N                ← trailing directive byte; ignored
        Completion ended ← optional trailing trace line; ignored

    Blank lines are dropped.
    """
    results: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        if not line:
            continue
        # Trailing directive byte — always last non-blank line.
        if line.startswith(":") and line[1:].isdigit():
            continue
        # Some cobra builds append a "Completion ended with directive: …" line.
        if line.startswith("Completion ended"):
            continue
        if "\t" in line:
            value, _, desc = line.partition("\t")
        else:
            value, desc = line, ""
        results.append((value, desc))
    return results


# Module-level singleton + enable/disable API.  Default: enabled.

_cobra_fallback: CobraCompleter | None = None
_cobra_enabled: bool = True


def enable_cobra_fallback(*, timeout: float = 1.5) -> CobraCompleter:
    """Enable the cobra-protocol fallback.

    Returns the configured :class:`CobraCompleter`.  The default state is
    *enabled* — call this only to override the timeout.
    """
    global _cobra_fallback, _cobra_enabled
    _cobra_fallback = CobraCompleter(timeout=timeout)
    _cobra_enabled = True
    return _cobra_fallback


def disable_cobra_fallback() -> None:
    """Disable the cobra-protocol fallback for this session."""
    global _cobra_enabled
    _cobra_enabled = False


def get_cobra_fallback() -> CobraCompleter | None:
    """Return the active cobra fallback, or ``None`` if disabled.

    Lazily initialises on first call.
    """
    global _cobra_fallback
    if not _cobra_enabled:
        return None
    if _cobra_fallback is None:
        _cobra_fallback = CobraCompleter()
    return _cobra_fallback
