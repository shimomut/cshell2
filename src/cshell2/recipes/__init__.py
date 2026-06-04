"""Completion recipes for external commands.

Recipes provide TAB completion for system commands. Enable them in
~/.cshell2/config.py:

    from cshell2.recipes import enable
    enable("*")              # all built-in + user recipes
    enable("make", "git")    # or pick specific ones

Available recipes:
    aws       drives the AWS CLI v2 ``aws_completer`` binary; covers every
              service, operation, flag, and live AWS resource discovery
    awsut     AWS utility commands — console URL opening, recent cost report,
              ec2 / cloudwatch logs / cloudformation, and SageMaker HyperPod
              cluster operations under `awsut hyperpod ...`
    chmod     mode operands (common octal + symbolic) and file completion
    chown     USER / USER:GROUP completion (system users + groups), files
    cp        copy flags (BSD/macOS vs GNU/Linux), file completion
    curl      flag dictionary and HTTP-method choices for -X / --request
    df        disk-free filesystem usage
    du        disk usage with size options
    find      filters, type, time, size, actions
    git       subcommands, branches, remotes, stash refs, per-subcommand flags
    grep      search flags (also egrep / fgrep / rgrep)
    kill      signal options, PID completion from running processes
              (also registers pkill with process-name completion)
    ls        listing flags
    lsof      flag dictionary, PID completion for -p
    make      Makefile target names and flags
    mv        move flags (BSD/macOS vs GNU/Linux), file completion
    ps        select-by-PID/USER/GROUP flag dictionary
    rm        remove flags (BSD/macOS vs GNU/Linux), file completion
    rsync     flag dictionary, [user@]host:-aware path completion
    scp       flag dictionary, [user@]host:-aware path completion
    ssh       host completion from ~/.ssh/config and known_hosts, options
    tail      follow options and file completion
    tar       create/extract/list flags, archive-aware file completion
    terraform subcommand tree, workspace completion, .tfvars file hints
    top       sort-key choices and PID completion (BSD/macOS vs Linux/procps)
    zip       compression flags, file completion
    unzip     listing/extraction flags, archive-aware file completion

Recipes for external commands silently skip registration when the
underlying command is not available on ``PATH`` — ``enable("tar")`` on a
host without ``tar`` is a no-op rather than a hard failure.

Tools built on cobra (docker, kubectl, helm, gh, …) are handled
automatically by the cobra-protocol fallback — no recipe needed.  See
``CobraCompleter`` in ``cshell2.completion``.
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

    Pass ``"*"`` to enable all discoverable recipes (built-in + search path).

    Lookup order for each name:

    1. Built-in package (``cshell2.recipes.<name>``).
    2. Each directory in :data:`recipe_search_path` in order
       (default: ``~/.cshell2/recipes/``).

    Raises ``ImportError`` if the recipe is not found anywhere.
    """
    names = recipe_names
    if "*" in names:
        names = _discover_all_recipes()
    for name in names:
        module = _load_recipe(name)
        module.register()


def _discover_all_recipes() -> list[str]:
    """Return sorted list of all available recipe names (built-in + search path)."""
    found: set[str] = set()

    # Built-in recipes: .py files in this package's directory (excluding __init__).
    builtin_dir = Path(__file__).parent
    for p in builtin_dir.glob("*.py"):
        if p.stem != "__init__":
            found.add(p.stem)

    # User/extra recipes from search path.
    for directory in recipe_search_path:
        if directory.is_dir():
            for p in directory.glob("*.py"):
                if p.stem != "__init__":
                    found.add(p.stem)

    return sorted(found)


def _load_recipe(name: str):
    """Return the module for *name*, searching built-ins then recipe_search_path."""
    # 1. Try built-in package first.
    try:
        return import_module(f".{name}", package=__package__)
    except ImportError as e:
        # Only swallow the error if the recipe module itself is missing.
        # If a dependency (e.g. boto3) is missing, propagate the error.
        if e.name != f"{__package__}.{name}":
            raise

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
