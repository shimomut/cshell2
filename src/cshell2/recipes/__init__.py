"""Completion recipes for external commands.

Recipes provide TAB completion for system commands. Enable them selectively
in ~/.cshell2/config.py:

    from cshell2.recipes import enable
    enable("make", "git", "docker", "ssh", "kill", "tail")

Available recipes:
    aws     aws s3 subcommands (ls, cp, mv, sync, rm, mb, rb, presign, website),
            S3 URI completion (s3://bucket/key), per-subcommand flags
    df      disk-free filesystem usage
    docker  subcommands, running containers, images, per-subcommand flags
    du      disk usage with size options
    find    filters, type, time, size, actions
    git     subcommands, branches, remotes, stash refs, per-subcommand flags
    grep    search flags (also egrep / fgrep / rgrep)
    kill    signal options, PID completion from running processes
            (also registers pkill with process-name completion)
    ls      listing flags
    make    Makefile target names and flags
    ssh     host completion from ~/.ssh/config and known_hosts, options
    tail    follow options and file completion
"""

from __future__ import annotations

import importlib.util
from importlib import import_module
from pathlib import Path


def enable(*recipe_names: str) -> None:
    """Enable one or more completion recipes by name.

    Looks for each recipe first in the built-in cshell2.recipes package, then
    falls back to ~/.cshell2/recipes/<name>.py for user-defined recipes.
    """
    for name in recipe_names:
        module = _load_recipe(name)
        module.register()


def _load_recipe(name: str):
    """Return the module for *name*, preferring built-ins over user recipes."""
    # 1. Try built-in package first.
    try:
        return import_module(f".{name}", package=__package__)
    except ImportError:
        pass

    # 2. Fall back to ~/.cshell2/recipes/<name>.py.
    user_path = Path.home() / ".cshell2" / "recipes" / f"{name}.py"
    if not user_path.exists():
        raise ImportError(
            f"Recipe {name!r} not found in built-in recipes or {user_path}"
        )

    spec = importlib.util.spec_from_file_location(f"cshell2_user_recipe_{name}", user_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
