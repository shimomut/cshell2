"""Command registry and @command decorator."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Callable

from .completion import Completer


@dataclass
class Command:
    name: str
    func: Callable
    completers: dict[int, Completer] = field(default_factory=dict)
    help_text: str = ""


class CommandRegistry:
    def __init__(self):
        self._commands: dict[str, Command] = {}
        self._external_completers: dict[str, dict[int, Completer]] = {}
        self._builtin_names: set[str] = set()

    def command(
        self,
        name: str | None = None,
        completers: dict[int, Completer] | None = None,
    ):
        """Decorator to register a Python function as a shell command."""
        def decorator(func: Callable) -> Callable:
            cmd_name = name or func.__name__
            cmd = Command(
                name=cmd_name,
                func=func,
                completers=completers or {},
                help_text=inspect.getdoc(func) or "",
            )
            self._commands[cmd_name] = cmd
            return func
        return decorator

    def register(
        self,
        func: Callable,
        name: str | None = None,
        completers: dict[int, Completer] | None = None,
    ) -> None:
        """Imperative registration (alternative to decorator)."""
        cmd_name = name or func.__name__
        cmd = Command(
            name=cmd_name,
            func=func,
            completers=completers or {},
            help_text=inspect.getdoc(func) or "",
        )
        self._commands[cmd_name] = cmd

    def register_external_completers(
        self,
        command_name: str,
        completers: dict[int, Completer],
    ) -> None:
        """Register completers for an external (system) command."""
        self._external_completers[command_name] = completers

    def get_external_completers(self, command_name: str) -> dict[int, Completer] | None:
        return self._external_completers.get(command_name)

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def list_commands(self) -> list[str]:
        return list(self._commands.keys())

    def has(self, name: str) -> bool:
        return name in self._commands

    def mark_builtins(self) -> None:
        """Snapshot current commands as builtins (won't be removed on reload)."""
        self._builtin_names = set(self._commands.keys())

    def clear_user_commands(self) -> None:
        """Remove all non-builtin commands and external completers."""
        self._commands = {
            k: v for k, v in self._commands.items() if k in self._builtin_names
        }
        self._external_completers.clear()


registry = CommandRegistry()
