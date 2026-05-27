# Writing Completion Recipes

A **recipe** adds TAB completion to an external (system) command — one that runs as a subprocess rather than a Python function registered with `@registry.command`. Recipes live in `src/cshell2/recipes/` and are activated in the user's config with `enable("name")`.

## Anatomy of a Recipe File

Every recipe file must expose a single `register()` function (no arguments). `enable("name")` imports the module and calls it; the recipe imports the module-level `registry` singleton directly:

```python
# src/cshell2/recipes/mytool.py

from ..commands import registry
from ..completion import OptionsCompleter, FileCompleter

def register() -> None:
    registry.register_external_completers("mytool", {
        None: OptionsCompleter({"-v": "verbose", "-o": "output file"},
                               args={"-o": "FILE"}),
        0: FileCompleter(),
    })
```

The dict passed to `register_external_completers` follows the same `{None: options, N: positional}` shape that the registry builds internally for `@registry.command(params=[...])`:

| Key | Completer activated when… |
|-----|--------------------------|
| `None` | User types a `-`-prefixed token at any argument position |
| `0`, `1`, `2`, … | User is completing argument at that index |

## Patterns

The recipes range from one-liners to complex multi-class files. Pick the pattern that fits the command.

---

### Pattern 1 — Flags only

Use this for simple commands whose only useful completions are flags. `OptionsCompleter` gives you multi-select TUI for free.

```python
# Example: ls recipe (abbreviated)
from ..commands import registry
from ..completion import FileCompleter, OptionsCompleter

LS_OPTIONS = {
    "-l": "long listing format",
    "-a": "include hidden files",
    "-h": "human-readable sizes",
    "-r": "reverse sort order",
    "-t": "sort by modification time",
}

def register() -> None:
    registry.register_external_completers("ls", {
        None: OptionsCompleter(LS_OPTIONS),
        0: FileCompleter(),
        1: FileCompleter(),
        2: FileCompleter(),
    })
```

Register the file completer at enough positional indices to cover realistic usage. Each argument slot costs one index regardless of whether a flag occupied that slot — because flag-value pairs count as two arguments in `ctx.args`.

---

### Pattern 2 — Flags with value arguments

When a flag takes a value (e.g. `-d N`, `-C DIR`), declare it in the `args` parameter of `OptionsCompleter`. Two forms are supported:

```python
args={
    "-j": "N",                              # hint only — user types the value
    "-C": ("DIR", DirCompleter()),          # hint + value completer — opens a picker
    "-f": ("FILE", FileCompleter()),        # hint + value completer
}
```

The engine detects when the user has just completed such a flag and either shows a hint line (hint-only form) or immediately opens the appropriate picker (completer form). The distinction: use a hint when the value is free-form (a number, a pattern); use a completer when there's a finite set of sensible values.

```python
# Example: make recipe (abbreviated)
from ..commands import registry
from ..completion import DirCompleter, FileCompleter, OptionsCompleter

MAKE_OPTIONS = {
    "-C": "change to directory before doing anything",
    "-f": "read FILE as the Makefile",
    "-j": "number of parallel jobs",
    "-n": "dry run — print commands without executing",
}

MAKE_ARGS = {
    "-C": ("DIR", DirCompleter()),   # opens a directory picker
    "-f": ("FILE", FileCompleter()), # opens a file picker
    "-j": "N",                       # hint only
}

def register() -> None:
    target_completer = MakeTargetCompleter()
    registry.register_external_completers("make", {
        None: OptionsCompleter(MAKE_OPTIONS, args=MAKE_ARGS),
        **{i: target_completer for i in range(8)},  # cover 4 flag+value pairs
    })
```

> **Why register many positional indices?** Flags consume argument slots. `make -C /tmp -j 4 test` has `["-C", "/tmp", "-j", "4"]` as `ctx.args` when the user starts typing `test`, so `arg_index` is 4. Registering up to index 7 (covering four flag+value pairs) handles most realistic invocations. Check the actual recipe for the right upper bound.

---

### Pattern 3 — Dynamic completions from a subprocess

When completions depend on live system state, call the appropriate tool in a `Completer` subclass. Always use `timeout` and catch `OSError`/`TimeoutExpired` so a slow or missing tool doesn't hang the prompt.

```python
import subprocess
from ..completion import Completer, Completion, CompletionContext

class RunningContainerCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
                capture_output=True, text=True, timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        prefix = ctx.prefix
        completions = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            name = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            if name.startswith(prefix):
                completions.append(Completion(value=name, description=desc))
        return completions
```

A shared `_run_tool(args, timeout)` helper in the module keeps error handling in one place:

```python
def _run_docker(args: list[str], timeout: float = 3.0) -> list[str]:
    try:
        result = subprocess.run(
            ["docker"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []
```

---

### Pattern 4 — Static subcommand list + per-subcommand dispatch

Commands with subcommands (git, docker) need two things: a subcommand completer at position 0, and a dispatcher at later positions that reads `ctx.args[0]` to choose what to complete.

```python
GIT_SUBCOMMANDS = {
    "commit": "record changes to the repository",
    "push":   "update remote refs",
    "pull":   "fetch and integrate",
    "branch": "list, create, or delete branches",
    # ...
}

class GitSubcommandCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=sub, description=desc)
            for sub, desc in GIT_SUBCOMMANDS.items()
            if sub.startswith(ctx.prefix)
        ]

class GitArgCompleter(Completer):
    """Dispatches to the right completer based on the git subcommand."""
    _branch  = GitBranchCompleter()
    _file    = FileCompleter()
    _remote  = GitRemoteCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        subcmd = ctx.args[0]
        if subcmd in ("checkout", "switch", "merge"):
            return self._branch.complete(ctx)
        if subcmd in ("add", "restore", "rm"):
            return self._file.complete(ctx)
        if subcmd in ("push", "fetch") and ctx.arg_index == 1:
            return self._remote.complete(ctx)
        if subcmd in ("push", "fetch"):
            return self._branch.complete(ctx)
        return []

def register() -> None:
    registry.register_external_completers("git", {
        None: GitSubcommandOptionsCompleter(),
        0: GitSubcommandCompleter(),
        1: GitArgCompleter(),
        2: GitArgCompleter(),   # same dispatcher works at all subsequent positions
        3: GitArgCompleter(),
    })
```

---

### Pattern 5 — Per-subcommand flags

When a command's flags differ by subcommand, use a custom completer under the `None` key instead of a plain `OptionsCompleter`. Override `should_activate` to keep the `-` guard:

```python
_SUBCOMMAND_OPTIONS = {
    "commit": {"--amend": "amend the tip commit", "-m": "commit message", ...},
    "push":   {"--force": "force push", "--dry-run": "dry run", ...},
    "log":    {"--oneline": "one line per commit", "--graph": "ASCII graph", ...},
}

class GitSubcommandOptionsCompleter(Completer):
    def should_activate(self, ctx: CompletionContext) -> bool:
        return ctx.prefix.startswith("-")

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        options = _SUBCOMMAND_OPTIONS.get(ctx.args[0])
        if not options:
            return []
        return [
            Completion(value=flag, description=desc, multi_select=True)
            for flag, desc in sorted(options.items())
            if flag.startswith(ctx.prefix)
        ]
```

Setting `multi_select=True` on every returned completion tells the line editor to open `InlineMultiPicker`. If you set it on only some completions the single-select `InlinePicker` is used instead.

---

### Pattern 6 — Two-level command groups (docker image ls, docker container run)

Some commands nest two levels deep. One clean approach: key the options dict by a `(group, subcmd)` tuple and look up with the right key in the options completer:

```python
_SUBCOMMAND_OPTIONS: dict[str | tuple[str, str], dict[str, str]] = {
    "run":              {"--rm": "auto-remove", "-d": "detach", ...},   # flat
    ("image",    "ls"): {"--all": "all images", "-q": "IDs only", ...}, # nested
    ("container","rm"): {"--force": "force", "-v": "remove volumes", ...},
}

class DockerSubcommandOptionsCompleter(Completer):
    def should_activate(self, ctx: CompletionContext) -> bool:
        return ctx.prefix.startswith("-")

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        first = ctx.args[0]
        if first in _MANAGEMENT_GROUPS and len(ctx.args) >= 2:
            key = (first, ctx.args[1])
        else:
            key = first
        options = _SUBCOMMAND_OPTIONS.get(key, {})
        return [
            Completion(value=f, description=d, multi_select=True)
            for f, d in sorted(options.items())
            if f.startswith(ctx.prefix)
        ]
```

---

### Pattern 7 — Multiple commands in one recipe

A single recipe can register completers for several related commands:

```python
def register() -> None:
    registry.register_external_completers("kill", {
        None: OptionsCompleter(KILL_OPTIONS),
        0: ProcessCompleter(),   # completes PIDs
        1: ProcessCompleter(),
    })
    registry.register_external_completers("pkill", {
        0: ProcessNameCompleter(),   # completes process names
    })
```

---

## Checklist for a New Recipe

1. **Create `src/cshell2/recipes/<name>.py`** with a `register()` function (no arguments — import `registry` from `..commands`).
2. **Add the recipe name** to the docstring in `src/cshell2/recipes/__init__.py` (the `Available recipes:` list).
3. **Cover common flags** with `OptionsCompleter`. Include both long and short forms where both exist.
4. **Use `args=` for value-taking flags** — hint-only `"N"` for numerics/free-text, `("HINT", SomeCompleter())` when a picker is useful.
5. **Register enough positional indices** to cover realistic flag+value combinations before the main argument.
6. **Protect subprocess calls** with `timeout` and catch `OSError`/`TimeoutExpired`.
7. **Don't cache at module level** (module is imported once per `enable()` call). Cache inside completer instances or use a module-level dict keyed by the relevant state.
8. **Test it** by enabling it in `~/.cshell2/config.py` and exercising TAB at each argument position.

## Completers Available for Recipes

| Completer | Import | Use for |
|-----------|--------|---------|
| `FileCompleter()` | `completion` | Any file argument |
| `DirCompleter()` | `completion` | Directory-only arguments (`-C DIR`) |
| `ChoiceCompleter(list)` | `completion` | Static value lists |
| `OptionsCompleter(dict, args)` | `completion` | Flags at any position |
| `ConditionalCompleter(mapping)` | `completion` | Values that depend on a preceding arg |
| Custom `Completer` subclass | — | Live data from subprocesses or files |
