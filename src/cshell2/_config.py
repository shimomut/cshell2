# cshell2 user configuration
# Define custom commands and completers here.

# ── Simple example: one positional argument ───────────────────────────────────

from cshell2.commands import registry, arg
from cshell2.completion import ChoiceCompleter

@registry.command(
    name="hello",
    help="Greet someone by name.",        # shell-facing description; no docstring needed
    params=[arg("name", nargs="?", default="world", completer=ChoiceCompleter(["world", "there"]))],
)
def hello(name):
    print(f"Hello, {name}!")


# ── Multi-level sub-command example ───────────────────────────────────────────
#
# Build a command tree by:
# 1. Calling `registry.command(name, ...)` bare — returns a `Command` node.
# 2. Chaining `.command(...)` on the node for each child (group or leaf).
# 3. Using `@parent.command(name, ...)` as a decorator to attach a handler.
#
# Flags declared on a parent node are *inherited* by every leaf automatically;
# a leaf handler only needs to accept the kwargs it actually uses — the rest
# are filtered out before the call.  So `--verbose` and `--dry-run` declared on
# the `deploy` root are usable on `deploy app`, `deploy rollback`, etc.

deploy = registry.command(
    "deploy",
    help="Deploy services to environments.",
    params=[
        # Inherited shared flags — visible on every sub-command.
        arg("-v", "--verbose", action="store_true",
                               help="print details for each step"),
        arg("-n", "--dry-run", action="store_true",
                               help="show steps, skip execution"),
    ],
)


@deploy.command(
    "app",
    help="Deploy a service to an environment.",
    params=[
        # choices= drives argparse validation AND TAB completion simultaneously.
        arg("environment", choices=["prod", "staging", "dev"]),
        arg("service",     nargs="?", default="all",
                           choices=["api", "web", "worker"]),
        # Value-taking flags: completer= drives TAB completion for the value.
        arg("-t", "--timeout", type=int, default=60, metavar="SECONDS",
                               help="deployment timeout in seconds",
                               completer=ChoiceCompleter(["30", "60", "120", "300"])),
        arg("-b", "--branch",  default="main",       metavar="BRANCH",
                               help="git branch to deploy"),
    ],
)
def deploy_app(environment, service, dry_run, verbose, timeout, branch):
    import time
    # Inherited flags (dry_run, verbose) arrive as kwargs alongside this leaf's own.
    # While this runs, press Ctrl+] to switch context without killing the deploy.
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Deploying '{service}' to '{environment}'  "
          f"branch={branch!r}  timeout={timeout}s")
    for step, secs in [("Build image",       2),
                       ("Push to registry",   3),
                       ("Update deployment",  2),
                       ("Wait for rollout",   4),
                       ("Health checks",      2)]:
        if verbose:
            print(f"  -> {step} ...", flush=True)
        if not dry_run:
            # Use short sleep intervals so Ctrl-C is handled promptly.
            for _ in range(secs * 10):
                time.sleep(0.1)
        print(f"  ok {step}")
    print(f"{prefix}Done.")


@deploy.command(
    "rollback",
    help="Roll back a deployment to the previous revision.",
    params=[
        arg("environment", choices=["prod", "staging", "dev"]),
        arg("service",     nargs="?", default="all",
                           choices=["api", "web", "worker"]),
    ],
)
def deploy_rollback(environment, service, dry_run, verbose):
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Rolling back '{service}' in '{environment}'")
    if verbose:
        print("  -> fetching previous revision ...")
        print("  -> swapping pointer ...")
    print(f"{prefix}Done.")


@deploy.command(
    "status",
    help="Show deployment status for an environment.",
    params=[
        arg("environment", choices=["prod", "staging", "dev"]),
    ],
)
def deploy_status(environment, verbose):
    # Leaf only declares the inherited flags it cares about (verbose);
    # dry_run is silently dropped because it's not in the signature.
    print(f"Status for '{environment}':")
    for name, ver in [("api", "v1.4.2"), ("web", "v1.4.2"), ("worker", "v1.4.1")]:
        print(f"  {name:8} {ver}")
        if verbose:
            print(f"    deployed: 2026-05-26 14:32 UTC")


# ── Enable completion recipes for external commands ───────────────────────────

from cshell2.recipes import enable
enable("*")


# ── Customize the prompt ──────────────────────────────────────────────────────

import os
from datetime import datetime
from cshell2 import set_prompt

def my_prompt(context_manager):
    """Replicates the built-in default prompt: [context] parent/cwd HH:MM:SS [bg:N]>."""
    CYAN_BOLD = "\033[1;36m"
    BLUE_BOLD = "\033[1;34m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RESET = "\033[0m"

    parts = []

    ctx = context_manager.current()
    if ctx and ctx.name != "default":
        parts.append(f"{CYAN_BOLD}[{ctx.name}]{RESET}")

    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd == home:
        short_path = "~"
    elif cwd.startswith(home + os.sep):
        rel = cwd[len(home) + 1:]
        rel_parts = rel.split(os.sep)
        if len(rel_parts) <= 2:
            short_path = "~/" + rel
        else:
            short_path = os.sep.join(rel_parts[-2:])
    else:
        abs_parts = cwd.lstrip(os.sep).split(os.sep)
        if len(abs_parts) <= 2:
            short_path = "/" + os.sep.join(abs_parts)
        else:
            short_path = os.sep.join(abs_parts[-2:])

    timestamp = datetime.now().strftime("%H:%M:%S")
    parts.append(f"{BLUE_BOLD}{short_path}{RESET}")
    parts.append(f"{GREEN}{timestamp}{RESET}")

    bg_count = 0
    current_name = context_manager.current_name
    for name, c in context_manager.contexts.items():
        if name != current_name and c.process_slot and c.process_slot.is_alive():
            bg_count += 1
    if bg_count:
        parts.append(f"{YELLOW}[bg:{bg_count}]{RESET}")

    return " ".join(parts) + "> "

set_prompt(my_prompt)
