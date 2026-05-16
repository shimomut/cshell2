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

### ConditionalCompleter

Selects a sub-completer based on the preceding arguments. Useful when argument N's valid values depend on what was chosen for arguments 0..N-1:

```python
ConditionalCompleter({
    ("prod",): ChoiceCompleter(["us-east-1", "us-west-2"]),
    ("staging",): ChoiceCompleter(["us-west-2"]),
})
```

Performs longest-prefix matching on `ctx.args` against the mapping keys.

## prompt_toolkit Integration

The `ShellCompleter` class in `shell.py` bridges cshell2's completion engine to prompt_toolkit:

```
prompt_toolkit calls get_completions(document, event)
  → Extract line text from document
  → Parse with split_for_completion(line) → (tokens, prefix)
  → Build CompletionContext
  → Route to appropriate completer
  → Convert Completion → PTKCompletion (with start_position, display, display_meta)
```

### Routing Logic

1. **No tokens parsed** → completing a command name → `CommandNameCompleter`
2. **Tokens present** → completing an argument:
   - Look up command in registry
   - Check `cmd.completers[arg_index]`
   - If completer exists and `should_activate()` → use it
   - If no completions and no completer registered → fall back to `FileCompleter`

The fallback to `FileCompleter` only triggers when no completer is registered for that position. If a completer is registered but returns empty results, no fallback occurs — this is intentional so commands can explicitly declare "no completions for this argument" by registering a completer that returns `[]`.

## Per-Argument Binding

Commands declare completers as a dict mapping argument index to completer:

```python
@registry.command(
    name="deploy",
    completers={
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
- Gaps are allowed (position 0 and 2 but not 1 → position 1 falls back to file completion)
- Later completers see earlier args via `ctx.args`

## Writing Custom Completers

### Basic Pattern

```python
class MyCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # Filter by prefix
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
        self._cache = {}

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        key = tuple(ctx.args[:2])
        if key not in self._cache:
            self._cache[key] = self._fetch(ctx.args[0], ctx.args[1])
        return [
            Completion(value=v)
            for v in self._cache[key]
            if v.startswith(ctx.prefix)
        ]
```

## Parsing for Completion

`split_for_completion(line)` splits the input line into tokens and a trailing prefix:

- `"git commit "` → `(["git", "commit"], "")`
- `"git commit -m "hel"` → `(["git", "commit", "-m"], "hel")`
- `"git "` → `(["git"], "")`
- `"gi"` → `([], "gi")`

The distinction between completed tokens (in `args`) and the in-progress token (in `prefix`) is critical for routing completions correctly.
