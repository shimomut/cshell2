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

# Directories searched in order when a recipe is not found in the built-in
# package.  The default entry covers the conventional user recipe location;
# call add_recipe_path() to register additional directories.
recipe_search_path: list[Path] = [Path.home() / ".cshell2" / "recipes"]


def add_recipe_path(path: str | Path) -> None:
    """Append *path* to the recipe search path.

    Recipes in directories added earlier in the list take priority over those
    added later.  The built-in package always has the highest priority.

    Example (in ~/.cshell2/config.py)::

        from cshell2.recipes import add_recipe_path, enable
        add_recipe_path("/team/shared/recipes")
        enable("my_tool")   # found in ~/.cshell2/recipes/ or /team/shared/recipes/
    """
    recipe_search_path.append(Path(path))


def enable(*recipe_names: str) -> None:
    """Enable one or more completion recipes by name.

    Lookup order for each name:

    1. Built-in package (``cshell2.recipes.<name>``).
    2. Each directory in :data:`recipe_search_path` in order
       (default: ``~/.cshell2/recipes/``).

    Raises ``ImportError`` if the recipe is not found anywhere.
    """
    for name in recipe_names:
        module = _load_recipe(name)
        module.register()


def _load_recipe(name: str):
    """Return the module for *name*, searching built-ins then recipe_search_path."""
    # 1. Try built-in package first.
    try:
        return import_module(f".{name}", package=__package__)
    except ImportError:
        pass

    # 2. Walk recipe_search_path; return the first match.
    for directory in recipe_search_path:
        candidate = Path(directory) / f"{name}.py"
        if candidate.exists():
            spec = importlib.util.spec_from_file_location(
                f"cshell2_user_recipe_{name}", candidate
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

    # 3. Not found anywhere.
    searched = ", ".join(str(d) for d in recipe_search_path)
    raise ImportError(
        f"Recipe {name!r} not found in built-in recipes or search path: [{searched}]"
    )
