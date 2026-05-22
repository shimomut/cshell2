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

from importlib import import_module

from ..commands import registry


def enable(*recipe_names: str) -> None:
    """Enable one or more completion recipes by name."""
    for name in recipe_names:
        module = import_module(f".{name}", package=__package__)
        module.register(registry)
