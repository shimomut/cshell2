# cshell2 user configuration
# Define custom commands and completers here.

# ── Simple example: one positional argument ───────────────────────────────────

from cshell2.commands import registry as command_registry, arg
from cshell2.completion import ChoiceCompleter

@command_registry.command(
    name="hello",
    help="Greet someone by name.",        # shell-facing description; no docstring needed
    params=[arg("name", nargs="?", default="world", completer=ChoiceCompleter(["world", "there"]))],
)
def hello(name):
    print(f"Hello, {name}!")


# ── Multi-level sub-command example ───────────────────────────────────────────
#
# Build a command tree by:
# 1. Calling `command_registry.command(name, ...)` bare — returns a `Command` node.
# 2. Chaining `.command(...)` on the node for each child (group or leaf).
# 3. Using `@parent.command(name, ...)` as a decorator to attach a handler.
#
# Flags declared on a parent node are *inherited* by every leaf automatically;
# a leaf handler only needs to accept the kwargs it actually uses — the rest
# are filtered out before the call.  So `--verbose` and `--dry-run` declared on
# the `deploy` root are usable on `deploy app`, `deploy rollback`, etc.

deploy = command_registry.command(
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


# ── Aliases ───────────────────────────────────────────────────────────────────
#
# Aliases expand the first token of a command line (bash-style):
#
#     hp create ...   →  awsut hyperpod create ...
#     la /tmp         →  ls -la /tmp
#
# They participate in TAB completion: typing the alias name and pressing TAB
# completes the rest as if the expansion had been typed.

command_registry.alias("hp", "awsut hyperpod")
command_registry.alias("la", "ls -la")


# ── Python-backed variables ───────────────────────────────────────────────────
#
# Register Vars to give `var NAME=VALUE` custom set logic and TAB completion
# for the value side.  Two flavours:
#
#   * EnvVar         — 1-to-1 passthrough to a single os.environ key.
#   * Var subclass   — full control: write multiple env keys, validate, etc.
#
# At the prompt:
#
#     cshell2> var editor=<TAB>           → vim, emacs, nano, code
#     cshell2> var editor=vim
#     cshell2> var http_proxy=http://...  → sets HTTP_PROXY *and* HTTPS_PROXY

import os
from cshell2.variables import registry as var_registry, EnvVar, Var

# Simple case: one logical name → one env var, with completion.
var_registry.register(EnvVar(
    name="editor",
    env_var="EDITOR",
    completer=ChoiceCompleter(["vim", "emacs", "nano", "code"]),
    description="Default text editor",
))


# Custom case: one logical name → two env keys.  Subclass Var when EnvVar
# isn't enough — e.g. when set() needs to write multiple env keys, validate
# the input, or trigger a side effect.
class _HttpProxyVar(Var):
    """Sets HTTP_PROXY and HTTPS_PROXY together from one logical name."""

    @property
    def name(self) -> str:
        return "http_proxy"

    @property
    def description(self) -> str:
        return "HTTP/HTTPS proxy — sets HTTP_PROXY + HTTPS_PROXY"

    @property
    def env_keys(self) -> list[str]:
        # Listed env keys are saved/restored on context switch.
        return ["HTTP_PROXY", "HTTPS_PROXY"]

    def get(self) -> str | None:
        return os.environ.get("HTTP_PROXY")

    def set(self, value: str) -> None:
        os.environ["HTTP_PROXY"] = value
        os.environ["HTTPS_PROXY"] = value

var_registry.register(_HttpProxyVar())


# ── Customize the prompt ──────────────────────────────────────────────────────

from datetime import datetime
from cshell2 import set_prompt

def my_prompt(context_manager):
    """Replicates the built-in default prompt: [context] parent/cwd HH:MM:SS [bg:N]>."""
    CYAN_BOLD = "\033[1m\033[38;2;0;188;212m"
    BLUE_BOLD = "\033[1m\033[38;2;100;149;237m"
    GREEN = "\033[38;2;80;200;100m"
    YELLOW = "\033[38;2;229;192;123m"
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


# ── Color scheme ──────────────────────────────────────────────────────────────
#
# Choose a built-in scheme or define a fully custom one.
# Applies to both the prompt colors and the TUI picker colors.
# Built-in schemes: "dark" (default), "light".
#
# Uncomment one of the examples below:

from cshell2 import set_color_scheme, ColorScheme

# Built-in schemes (for light- or dark-background terminals):
# set_color_scheme("dark")   # default
# set_color_scheme("light")

# Fully custom scheme — specify any subset of colors as (R, G, B) tuples:
# set_color_scheme(ColorScheme(
#     prompt_context=(180, 100, 255),      # context name in prompt
#     prompt_path=(100, 149, 237),         # cwd in prompt
#     prompt_time=(80, 200, 100),          # timestamp in prompt
#     prompt_bg_count=(229, 192, 123),     # [bg:N] indicator in prompt
#     picker_row_bg=(50, 50, 60),          # non-selected picker row background
#     picker_row_fg=(220, 220, 220),       # non-selected picker row foreground
#     picker_sel_bg=(80, 40, 160),         # selected row background
#     picker_sel_fg=(255, 255, 255),       # selected row foreground
#     picker_scroll_thumb=(120, 120, 120), # scrollbar thumb
#     picker_scroll_track=(40, 40, 50),    # scrollbar track
# ))
