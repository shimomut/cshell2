"""Completion engine — Completer ABC, CompletionContext, built-in completers."""

from __future__ import annotations

import os
import shutil
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
