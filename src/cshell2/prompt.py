"""Customizable prompt generation."""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .context import ContextManager

PromptFunc = Callable[["ContextManager"], str]

_prompt_func: PromptFunc | None = None


def set_prompt(func: PromptFunc) -> None:
    """Set a custom prompt function.

    The function receives the ContextManager and returns the prompt string.
    """
    global _prompt_func
    _prompt_func = func


def get_prompt_func() -> PromptFunc:
    """Return the active prompt function (custom or default)."""
    return _prompt_func or default_prompt


def default_prompt(context_manager: "ContextManager") -> str:
    """Default prompt: [context] parent/cwd HH:MM:SS> """
    parts = []

    ctx = context_manager.current()
    if ctx:
        parts.append(f"[{ctx.name}]")

    cwd = os.getcwd()
    path = os.path.normpath(cwd)
    components = path.split(os.sep)
    short_path = os.sep.join(components[-2:]) if len(components) >= 2 else path

    timestamp = datetime.now().strftime("%H:%M:%S")
    parts.append(short_path)
    parts.append(timestamp)

    return " ".join(parts) + "> "
