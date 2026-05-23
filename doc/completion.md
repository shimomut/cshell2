# Completion Engine Design

## Overview

The completion engine provides context-aware tab completion for all shell input. It is designed to be deeply customizable — each command declares completers for each argument position, and each completer receives full parse state to make intelligent suggestions.

## Core Types

### CompletionContext

Every completer receives a `CompletionContext` with full awareness of what's been typed:

```python
@dataclass
class CompletionContext:
    command: str | None        # command name (None if completing command itself)
    args: list[str]            # all preceding arguments (already completed)
    arg_index: int             # which argument position is being completed
    prefix: str               # partial text of current argument being completed
    line: str                 # full raw line
    shell_context: Context | None  # current shell context
```

The `shell_context` field gives completers access to the active context's variables (e.g., account, region), enabling completions that adapt to the current environment without explicit arguments.

### Completion

```python
@dataclass
class Completion:
    value: str              # the text inserted on selection
    display: str = ""       # label shown in completion menu (defaults to value)
    description: str = ""   # metadata shown beside the completion
    multi_select: bool = False   # True → opens InlineMultiPicker instead of InlinePicker
    combinable: bool = False     # True for single-char flags that can be merged (-a -l → -al)
    arg_hint: str = ""           # non-empty when flag requires a following argument (e.g. "N")
    is_arg_hint: bool = False    # True when this IS the hint for a preceding flag's value
```

### Completer Protocol

```python
class Completer(ABC):
    @abstractmethod
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        """Return completions for the current position."""
        ...

    def should_activate(self, ctx: CompletionContext) -> bool:
        """Optional guard — return False to skip this completer dynamically."""
        return True
```

The `should_activate` guard allows a completer to be registered at a position but only engage under certain conditions (e.g., only complete context names after `context switch`, not after `context list`).

## Built-in Completers

### FileCompleter

Completes filesystem paths relative to the current directory. Handles:
- Directory prefix expansion (`src/` lists contents of `src/`)
- Hidden file filtering (only shown when prefix starts with `.`)
- Directory suffix (`/` appended to directory completions)
- Case-insensitive matching

### DirCompleter

Like `FileCompleter` but only returns directories. Used for flags that take a directory path (e.g. `du -C DIR`).

### CommandNameCompleter

Completes command names from two sources:
1. Registered commands in the `CommandRegistry`
2. Executable files on `$PATH` (only searched when prefix is non-empty, to avoid flooding)

Results are labeled `"command"` or `"system"` in the description field.

### ChoiceCompleter

Completes from a static list of strings. Simple but covers many cases (subcommands, enum-like arguments, known account names).

```python
ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1"])
```

### CallbackCompleter

Completes from a function's return value. The function is called on each completion attempt, enabling dynamic lists:

```python
CallbackCompleter(lambda: get_current_branches())
```

### OptionsCompleter

Completes command-line flags. Registered under the `None` key in a completers dict so it activates at any argument position when the user types a `-`-prefixed token.

```python
OptionsCompleter(
    options={
        "-l": "long format",
        "-a": "show hidden",
        "--color": "colorize output",
        "-d": "max depth",
    },
    args={
        "-d": "N",               # hint only — user types the value
        "--color": ("WHEN", ChoiceCompleter(["always", "auto", "never"])),
        # tuple form: (hint, value_completer) → opens a picker for the value
    },
)
```

When all completions returned are `multi_select=True` (which `OptionsCompleter` always sets), the line editor opens `InlineMultiPicker` instead of `InlinePicker`. The user:
- Navigates with arrows / `Ctrl+P/N`
- **Space** to toggle a flag's checked state
- **Enter** to confirm (checked items, or highlighted item if nothing checked)
- Types a letter to jump to the next flag starting with that letter

Boolean short flags are automatically merged: selecting `-a` and `-l` inserts `-al`. Flags with `arg_hint` are inserted individually followed by a space, then either a value picker or an inline hint line.

`OptionsCompleter` also handles:
- **Flag deduplication** — flags already present in `ctx.args` are excluded
- **Short-flag cluster parsing** — `-hs` in `ctx.args` is treated as both `-h` and `-s` already used
- **Preceding-flag hint** — when the last completed arg is a value-taking flag and the user presses TAB without typing `-`, the engine shows a hint instead of opening a picker

### ConditionalCompleter

Selects a sub-completer based on the preceding arguments. Useful when argument N's valid values depend on what was chosen for arguments 0..N-1:

```python
ConditionalCompleter({
    ("prod",): ChoiceCompleter(["us-east-1", "us-west-2"]),
    ("staging",): ChoiceCompleter(["us-west-2"]),
})
```

Performs longest-prefix matching on `ctx.args` against the mapping keys: tries the full `args` tuple first, then progressively shorter prefixes.

## How TAB Completion Works

The line editor (`lineedit.py`) calls `_get_completions(line_before_cursor)` on every TAB press. The shell implements this as:

```
_get_completions(line_before_cursor)
  → _split_on_operators() → isolate current pipeline stage
  → split_for_completion(stage) → (tokens, prefix)
  → No tokens?
      → CommandNameCompleter
  → Has tokens?
      → Look up command in registry (or external completers)
      → completers[None] present AND prefix starts with "-"?
          → Check if last arg is a value-taking flag (preceding-flag hint)
              → Yes, has value_completer → return value_completer.complete(ctx)
              → Yes, hint only → return [is_arg_hint=True Completion]
          → options_completer.complete(ctx) if should_activate()
      → No options matches yet, completers[arg_index] present?
          → positional_completer.complete(ctx) if should_activate()
      → Still no matches and no completer registered? → FileCompleter fallback
```

Once completions are returned to the line editor:

| Situation | Behaviour |
|-----------|-----------|
| Zero completions | Do nothing |
| Single `is_arg_hint` completion | Show inline hint below buffer; cleared on next keypress |
| Single `multi_select` + `arg_hint` completion | Auto-apply the flag (insert `flag `), then loop again to handle the value |
| Single non-hint completion | Apply immediately; if it has `arg_hint`, then prompt for the value |
| All `multi_select` | Open `InlineMultiPicker` |
| Mixed | Open `InlinePicker` (narrows as user types more characters) |

The **fallback to `FileCompleter`** only triggers when **no completer** is registered for that position. If a completer is registered but returns empty results, no fallback occurs — commands can explicitly declare "no completions here" by registering a completer that returns `[]`.

## Per-Argument Binding

Commands declare completers as a dict mapping argument index (or `None` for options) to completer:

```python
@registry.command(
    name="deploy",
    completers={
        None: OptionsCompleter({"-v": "verbose", "--dry-run": "dry run"}),
        0: ChoiceCompleter(["prod", "staging"]),
        1: RegionCompleter(),
        2: ServiceCompleter(),  # can inspect ctx.args[0], ctx.args[1]
    }
)
def deploy(env, region, service):
    ...
```

This design means:
- Each position is independent — no need to declare all positions
- Gaps are allowed (positions 0 and 2 but not 1 → position 1 falls back to file completion)
- Later completers see earlier args via `ctx.args`
- `None` key activates at any position when the user types a `-` prefix

For system commands that should not be wrapped as Python functions:

```python
registry.register_external_completers("rsync", {
    None: OptionsCompleter({"-a": "archive", "-v": "verbose", "-n": "dry run",
                            "--exclude": "exclude pattern"},
                           args={"--exclude": "PATTERN"}),
    0: FileCompleter(),
    1: FileCompleter(),
})
```

## Writing Custom Completers

### Basic Pattern

```python
class MyCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=item, description=desc)
            for item, desc in self._get_items()
            if item.startswith(ctx.prefix)
        ]
```

### Context-Aware Pattern

```python
class EC2InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # Use preceding args or fall back to shell context
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("account")
        region = ctx.args[1] if len(ctx.args) > 1 else ctx.shell_context.get_variable("region")
        instances = fetch_instances(account, region)
        return [
            Completion(value=i["id"], description=i["name"])
            for i in instances
            if i["id"].startswith(ctx.prefix)
        ]
```

### Caching Pattern

For completers that call expensive APIs, cache results keyed on the relevant arguments:

```python
class CachedCompleter(Completer):
    def __init__(self):
        self._cache: dict[tuple, list[Completion]] = {}

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        key = tuple(ctx.args[:2])
        if key not in self._cache:
            self._cache[key] = self._fetch(ctx.args[0], ctx.args[1])
        return [c for c in self._cache[key] if c.value.startswith(ctx.prefix)]
```

## Parsing for Completion

`split_for_completion(line)` splits the input line into tokens and a trailing prefix:

- `"git commit "` → `(["git", "commit"], "")`
- `"git commit -m hel"` → `(["git", "commit", "-m"], "hel")`
- `"git "` → `(["git"], "")`
- `"gi"` → `([], "gi")`

The distinction between completed tokens (in `args`) and the in-progress token (in `prefix`) is critical for routing completions correctly.

Completion is always scoped to the **current pipeline stage**: for `ls | grep -`, the completion context uses `grep` as the command, not `ls`.
