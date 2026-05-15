# cshell2

A lightweight but powerful terminal shell environment implemented in Python.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                    cshell2                           │
├─────────────────────────────────────────────────────┤
│  Shell Loop (shell.py)                              │
│  ├── Input handling (prompt_toolkit or readline)    │
│  ├── Line parsing                                  │
│  ├── Command dispatch                              │
│  └── History management                            │
├─────────────────────────────────────────────────────┤
│  Command Registry (commands.py)                     │
│  ├── Built-in commands                             │
│  ├── Python function commands (from config)        │
│  └── System command passthrough                    │
├─────────────────────────────────────────────────────┤
│  Completion Engine (completion.py)                  │
│  ├── Command name completion                       │
│  ├── Argument completion (per-command completers)  │
│  └── Filesystem completion (fallback)              │
├─────────────────────────────────────────────────────┤
│  Context Manager (context.py)                       │
│  ├── Context stack                                 │
│  └── Context-aware variable resolution             │
├─────────────────────────────────────────────────────┤
│  User Config (~/.cshell2/config.py)                 │
│  ├── Custom command definitions                    │
│  └── Custom completer definitions                  │
└─────────────────────────────────────────────────────┘
```

## Module Design

### shell.py — Main Shell Loop

Entry point. Reads input, parses lines, dispatches commands.

- Uses `prompt_toolkit` for terminal input (rich completion UI, history search, key bindings)
- Falls back to `readline` if prompt_toolkit unavailable
- Supports `Ctrl+R` history search
- Maintains command history in `~/.cshell2/history`

### commands.py — Command Registry

```python
registry = CommandRegistry()

@registry.command(name="hello", completers=[None, UsernameCompleter()])
def hello(name: str):
    print(f"Hello, {name}!")
```

Dispatch order:
1. Built-in commands (cd, exit, context)
2. Registered Python function commands
3. System commands (via subprocess)

### completion.py — Completion Engine

The core of the TAB completion system. Designed for deep customization.

#### CompletionContext

Every completer receives a `CompletionContext` with full awareness of what's been typed:

```python
@dataclass
class CompletionContext:
    command: str | None        # command name (None if completing command itself)
    args: list[str]            # all preceding arguments (already completed)
    arg_index: int             # which argument position is being completed
    prefix: str               # partial text of current argument being completed
    line: str                 # full raw line
    shell_context: Context    # current shell context (account, region, etc.)
```

#### Completer Protocol

```python
class Completer(ABC):
    @abstractmethod
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        """Return completions for the current position."""
        ...

    def should_activate(self, ctx: CompletionContext) -> bool:
        """Optional guard — return False to skip this completer dynamically."""
        return True

@dataclass
class Completion:
    value: str              # the completion text
    display: str = ""       # optional display label (shown in menu)
    description: str = ""   # optional description (shown beside completion)
```

#### Built-in Completers

```python
class FileCompleter(Completer): ...       # filesystem paths
class CommandNameCompleter(Completer): ... # registered + system commands
class ChoiceCompleter(Completer):          # static list of choices
    def __init__(self, choices: list[str]): ...
```

#### Per-Argument Completer Binding

Commands declare a completer for each argument position. Completers at later positions can inspect preceding args via `ctx.args`:

```python
@registry.command(
    name="ssh_instance",
    completers={
        0: ChoiceCompleter(["account-A", "account-B"]),
        1: RegionCompleter(),
        2: EC2InstanceCompleter(),  # uses ctx.args[0] and ctx.args[1]
    }
)
def ssh_instance(account: str, region: str, instance_id: str):
    ...
```

#### Example: Context-Aware EC2 Completer

```python
class EC2InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if len(ctx.args) < 2:
            return []
        account_id = ctx.args[0]
        region = ctx.args[1]
        instances = self._fetch_instances(account_id, region)
        return [
            Completion(
                value=inst["InstanceId"],
                display=inst["InstanceId"],
                description=inst.get("Name", ""),
            )
            for inst in instances
        ]

    def _fetch_instances(self, account_id, region):
        # Call AWS API, cache results
        ...
```

#### Completer Composition

Completers can be combined for complex scenarios:

```python
class ConditionalCompleter(Completer):
    """Picks a sub-completer based on preceding args."""
    def __init__(self, mapping: dict[tuple, Completer]):
        self.mapping = mapping

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        key = tuple(ctx.args)
        completer = self.mapping.get(key)
        if completer:
            return completer.complete(ctx)
        return []
```

### context.py — Context Switch

Contexts represent an environment (e.g., AWS account + region, k8s cluster, project directory). Commands and completers see the active context.

The context manager holds a named collection of contexts with a "current" pointer. You can switch to any context by name without destroying others. Push/pop is available as a convenience for temporary context switches.

```python
class Context:
    name: str
    variables: dict[str, str]   # e.g. {"account": "123", "region": "us-west-2"}

class ContextManager:
    contexts: dict[str, Context]   # all known contexts by name
    current_name: str | None       # which context is active
    stack: list[str]               # history stack for push/pop (stores names)

    def create(self, name: str, **variables) -> Context: ...
    def switch(self, name: str): ...           # set current to any existing context
    def push(self, name: str): ...             # save current to stack, switch to name
    def pop(self) -> Context: ...              # switch back to previous on stack
    def current(self) -> Context | None: ...
    def list_contexts(self) -> list[str]: ...
    def remove(self, name: str): ...
    def set_variable(self, key, value): ...    # set on current context
    def get_variable(self, key) -> str | None: ...
```

Switching context:
```
cshell2> context create prod --account 123456 --region us-east-1
cshell2> context create staging --account 789012 --region us-west-2
cshell2> context switch prod
[prod] cshell2> context switch staging       # switch directly, prod is still available
[staging] cshell2> context switch prod       # jump back without pop
[prod] cshell2> context push staging         # stack-style: remember prod, switch to staging
[staging] cshell2> context pop               # back to prod
[prod] cshell2> context list                 # show all: prod*, staging
```

Completers can use `ctx.shell_context` to adapt:
```python
class EC2InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("account")
        region = ctx.args[1] if len(ctx.args) > 1 else ctx.shell_context.get_variable("region")
        ...
```

### User Config (~/.cshell2/config.py)

Users define their custom commands and completers here. Loaded at shell startup.

```python
# ~/.cshell2/config.py
from cshell2.commands import registry
from cshell2.completion import Completer, Completion, ChoiceCompleter

class MyInstanceCompleter(Completer):
    def complete(self, ctx):
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("account")
        # ... fetch and return completions
        return [Completion(value="i-abc123", description="web-server-1")]

@registry.command(
    name="connect",
    completers={
        0: ChoiceCompleter(["prod", "staging"]),
        1: ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1"]),
        2: MyInstanceCompleter(),
    }
)
def connect(account, region, instance_id):
    """SSH into an EC2 instance."""
    os.system(f"ssh {instance_id}")
```

## File Layout

```
cshell2/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── src/
│   └── cshell2/
│       ├── __init__.py
│       ├── __main__.py       # entry point
│       ├── shell.py          # main loop, input handling
│       ├── commands.py       # command registry, @command decorator
│       ├── completion.py     # Completer ABC, CompletionContext, built-in completers
│       ├── context.py        # Context, ContextManager
│       ├── history.py        # history storage and search
│       └── parsing.py        # line tokenization, quote handling
└── tests/
    ├── test_commands.py
    ├── test_completion.py
    ├── test_context.py
    └── test_parsing.py
```

## Key Design Decisions

1. **prompt_toolkit over raw readline** — gives us multi-line edit, colored completions with descriptions, async completion, and better cross-platform support.

2. **Completer receives full context** — the `CompletionContext` dataclass carries all parsed state so completers can make decisions based on command name, preceding args, and shell context without global state.

3. **Dict-based positional completers** — using `{arg_index: Completer}` lets each position have independent logic. A completer at position N can inspect `ctx.args[:N]` to see what was already chosen.

4. **Context as a stack** — push/pop semantics let you temporarily enter a context and return, useful for scripting and nested operations.

5. **Config as Python** — `config.py` is just Python that imports cshell2 APIs. No DSL to learn; full language power for defining completers with caching, API calls, etc.

6. **System command fallback** — anything not registered as a Python command is passed to the system shell, so cshell2 is a drop-in replacement for daily use.
