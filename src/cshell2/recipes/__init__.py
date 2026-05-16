"""Completion recipes for external commands.

Recipes provide TAB completion for system commands (make, git, docker, etc.).
Enable them selectively in ~/.cshell2/config.py:

    from cshell2.recipes import enable
    enable("make")
"""

from __future__ import annotations

from importlib import import_module

from ..commands import registry


def enable(*recipe_names: str) -> None:
    """Enable one or more completion recipes by name."""
    for name in recipe_names:
        module = import_module(f".{name}", package=__package__)
        module.register(registry)
