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

    def __post_init__(self):
        if not self.display:
            self.display = self.value


class Completer(ABC):
    @abstractmethod
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        ...

    def should_activate(self, ctx: CompletionContext) -> bool:
        return True


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
