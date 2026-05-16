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

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def list_commands(self) -> list[str]:
        return list(self._commands.keys())

    def has(self, name: str) -> bool:
        return name in self._commands


registry = CommandRegistry()
