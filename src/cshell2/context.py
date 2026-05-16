"""Context management — named collection with current pointer and push/pop stack.

Each context stores:
- variables: exported to os.environ on switch
- cwd: saved/restored on switch
- process_slot: optional running subprocess (for multiplexing)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .process import ProcessSlot

_SENTINEL = object()


class ContextState(Enum):
    IDLE = auto()
    RUNNING = auto()
    EXITED = auto()


@dataclass
class Context:
    name: str
    variables: dict[str, str] = field(default_factory=dict)
    cwd: str = field(default_factory=os.getcwd)
    process_slot: ProcessSlot | None = field(default=None, repr=False)

    @property
    def state(self) -> ContextState:
        if self.process_slot is None:
            return ContextState.IDLE
        if self.process_slot.is_alive():
            return ContextState.RUNNING
        return ContextState.EXITED


class ContextManager:
    def __init__(self):
        self.contexts: dict[str, Context] = {}
        self.current_name: str | None = None
        self.stack: list[str] = []
        self._env_backup: dict[str, str | None] = {}
        self._initial_cwd: str = os.getcwd()

    def create(self, name: str, **variables: str) -> Context:
        ctx = Context(name=name, variables=variables, cwd=os.getcwd())
        self.contexts[name] = ctx
        if self.current_name is None:
            self.current_name = name
            self._apply_env(ctx)
        return ctx

    def switch(self, name: str) -> None:
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        self._save_current()
        self.current_name = name
        self._restore(self.contexts[name])

    def push(self, name: str) -> None:
        if self.current_name is not None:
            self.stack.append(self.current_name)
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        self._save_current()
        self.current_name = name
        self._restore(self.contexts[name])

    def pop(self) -> Context | None:
        if not self.stack:
            self._save_current()
            self._unapply_env()
            self.current_name = None
            os.chdir(self._initial_cwd)
            return None
        self._save_current()
        prev_name = self.stack.pop()
        self.current_name = prev_name
        ctx = self.contexts.get(prev_name)
        if ctx:
            self._restore(ctx)
        return ctx

    def current(self) -> Context | None:
        if self.current_name is None:
            return None
        return self.contexts.get(self.current_name)

    def list_contexts(self) -> list[str]:
        return list(self.contexts.keys())

    def remove(self, name: str) -> None:
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        was_current = self.current_name == name
        del self.contexts[name]
        self.stack = [n for n in self.stack if n != name]
        if was_current:
            self._unapply_env()
            self.current_name = self.stack[-1] if self.stack else None
            if self.current_name:
                self._restore(self.contexts[self.current_name])
            else:
                os.chdir(self._initial_cwd)

    def set_variable(self, key: str, value: str) -> None:
        ctx = self.current()
        if ctx is None:
            raise RuntimeError("No active context")
        ctx.variables[key] = value
        os.environ[key] = value

    def get_variable(self, key: str) -> str | None:
        ctx = self.current()
        if ctx is None:
            return None
        return ctx.variables.get(key)

    def _save_current(self) -> None:
        """Snapshot cwd into the current context before switching away."""
        if self.current_name is None:
            return
        ctx = self.contexts.get(self.current_name)
        if ctx:
            ctx.cwd = os.getcwd()

    def _restore(self, ctx: Context) -> None:
        """Apply a context: restore cwd and set env vars."""
        self._unapply_env()
        os.chdir(ctx.cwd)
        self._apply_env(ctx)

    def _apply_env(self, ctx: Context) -> None:
        """Export context variables to os.environ, backing up originals."""
        self._env_backup = {}
        for key, value in ctx.variables.items():
            self._env_backup[key] = os.environ.get(key)
            os.environ[key] = value

    def _unapply_env(self) -> None:
        """Restore os.environ to state before the current context was applied."""
        for key, original in self._env_backup.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        self._env_backup = {}
