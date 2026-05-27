# cshell2

A lightweight but powerful terminal shell environment implemented in Python.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                    cshell2                           │
├─────────────────────────────────────────────────────┤
│  Shell Loop (shell.py)                              │
│  ├── Input handling (lineedit.py — DIY raw editor)  │
│  ├── Line parsing / pipeline execution              │
│  ├── Command dispatch                              │
│  ├── PTY process multiplexing (process.py)         │
│  └── Context switch TUI (tui.py)                   │
├─────────────────────────────────────────────────────┤
│  Command Registry (commands.py)                     │
│  ├── Built-in commands                             │
│  ├── Python function commands (from config)        │
│  ├── External completer registration               │
│  └── System command passthrough                    │
├─────────────────────────────────────────────────────┤
│  Variable Registry (variables.py)                   │
│  ├── Var ABC — Python-backed shell variables       │
│  ├── VarRegistry + var_registry singleton          │
│  ├── EnvVar convenience subclass                   │
│  └── VarCompleter — KEY=VALUE completion for var   │
├─────────────────────────────────────────────────────┤
│  Completion Engine (completion.py)                  │
│  ├── Command name completion                       │
│  ├── Argument completion (per-command completers)  │
│  ├── Options completion (flags, multi-select TUI)  │
│  └── Filesystem completion (fallback)              │
├─────────────────────────────────────────────────────┤
│  TUI Widgets (tui.py)                               │
│  ├── InlinePicker — single-select inline list      │
│  ├── InlineMultiPicker — multi-select with Space   │
│  └── InlineArgPrompt — flag-argument text input    │
├─────────────────────────────────────────────────────┤
│  Line Editor (lineedit.py)                          │
│  ├── Raw-mode key dispatch                         │
│  ├── History (up/down, Ctrl+R search)              │
│  ├── TAB completion via InlinePicker               │
│  └── Ctrl+] context switch (inline picker)         │
├─────────────────────────────────────────────────────┤
│  Context Manager (context.py)                       │
│  ├── Context stack                                 │
│  ├── Context-aware variable resolution             │
│  ├── CWD save/restore on switch                    │
│  └── env var apply/unapply on switch               │
├─────────────────────────────────────────────────────┤
│  Prompt (prompt.py)                                 │
│  └── Default + user-overrideable prompt function   │
├─────────────────────────────────────────────────────┤
│  Recipes (recipes/)                                 │
│  └── Completion recipes for external commands      │
├─────────────────────────────────────────────────────┤
│  User Config (~/.cshell2/config.py)                 │
│  ├── Custom command definitions                    │
│  └── Custom completer definitions                  │
└─────────────────────────────────────────────────────┘
```

## Module Design

### shell.py — Main Shell Loop

Entry point. Reads input, parses lines, dispatches commands.

- Uses a DIY raw-mode line editor (`lineedit.py`) — no external dependencies
- Supports `Ctrl+R` history search (via inline picker)
- Supports `Ctrl+]` to open an inline context-switch picker
- Maintains command history in `~/.cshell2/history`
- Runs external commands in PTY-backed subprocess slots (`process.py`)
- Executes pipelines (`|`), sequences (`;`, `&&`, `||`), and redirections (`>`, `>>`, `<`, `2>`, `2>&1`)

**Built-in commands:** `cd`, `exit`, `reload`, `var`, `unset`, `help`, `context`

**Ctrl+] context switching:** The user can press `Ctrl+]` at the shell prompt (or during a running process) to open a TUI picker listing all contexts. Selecting a context with a live process resumes it immediately. Selecting `+ new context` prompts for a name and creates a new context inheriting the current context's variables. The shell tracks processes across context switches via `ProcessSlot` (see `process.py`).

### commands.py — Command Registry

```python
from cshell2.commands import registry, arg

@registry.command(
    name="hello",
    help="Greet someone by name.",
    params=[arg("name", completer=UsernameCompleter())],
)
def hello(name):
    print(f"Hello, {name}!")
```

Methods:
- `command(name, *, help=None, params=None)` — decorator to register a Python function. `params=[arg(...)]` declares positionals and flags; the registry derives both an argparse parser and the per-position completer dict from the same list.
- `register(cmd: Command)` — register a pre-built `Command` object (mirrors `var_registry.register(var_object)`)
- `register_external_completers(command_name, completers)` — attach a `{None: OptionsCompleter, N: positional}` dict to a system command (e.g. `git`, `docker`) without wrapping it as a Python command
- `mark_builtins()` — snapshot current commands as builtins (not removed on `reload`)
- `clear_user_commands()` — remove non-builtin commands and all external completers

Dispatch order:
1. Built-in commands (cd, exit, reload, var, unset, help, context)
2. Registered Python function commands
3. System commands (via PTY subprocess)

### variables.py — Variable Registry

Python-backed shell variables that mirror the `CommandRegistry` pattern. Users subclass `Var` and register instances with `var_registry`; the built-in `var` command dispatches through the registry instead of writing directly to `os.environ`.

#### `Var` ABC

```python
class Var(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Logical name as seen in the shell (e.g. 'aws_region')."""
        ...

    @abstractmethod
    def get(self) -> str | None:
        """Return current display value (shown by 'var' with no args)."""
        ...

    @abstractmethod
    def set(self, value: str) -> None:
        """Called when the user runs 'var aws_region=us-east-1'."""
        ...

    @property
    def value_completer(self) -> Completer | None:
        """Optional completer for the value side of KEY=VALUE."""
        return None

    @property
    def description(self) -> str:
        return ""
```

#### Built-in Convenience Subclass

```python
class EnvVar(Var):
    """1-to-1 passthrough to a single os.environ key, with an optional completer."""
    def __init__(self, name: str, env_var: str | None = None,
                 completer: Completer | None = None, description: str = ""): ...
```

When a variable needs to write multiple env keys (e.g. `AWS_REGION` + `AWS_DEFAULT_REGION`), subclass `Var` directly and implement `set()` and `env_keys` accordingly.

#### Registry

The module-level singleton is named `registry` inside `variables.py` (mirroring `commands.py`). Importers typically alias it as `var_registry` to disambiguate from the command registry:

```python
from cshell2.variables import registry as var_registry
# or, equivalently, `from cshell2 import var_registry`

# Methods:
var_registry.register(var: Var) -> None
var_registry.get(name: str) -> Var | None
var_registry.all() -> list[Var]
```

#### `var` Command Dispatch

When the user runs `var NAME=VALUE`, the `var` built-in checks `var_registry` first. If a `Var` is found for `NAME`, its `set()` method is called; otherwise the value is written directly to `os.environ` (legacy behaviour). Reading (`var NAME` with no `=`) calls `Var.get()`.

```
var                          → list all env vars + registered Vars with their get() values
var aws_region               → print current value via AwsRegionVar.get()
var aws_region=us-east-1     → dispatch to AwsRegionVar.set("us-east-1")
var AWS_SESSION_TOKEN=abc    → plain os.environ set (no Var registered — fallback)
```

#### `VarCompleter` — `=`-Aware Completion for `var`

Registered as the positional completer on the `var` command. It splits the current token at `=` to handle both phases:

- Typing `var aws_<TAB>` → list registered `Var` names (with `=` appended)
- Typing `var aws_region=<TAB>` → delegate to `AwsRegionVar.value_completer`
- Typing `var aws_region=us-<TAB>` → narrow the value list by prefix

The split is local to `VarCompleter`; the global tokenizer is not changed.

#### Example: Custom `Var` Subclass

```python
class AwsRegionVar(Var):
    name = "aws_region"
    description = "AWS region — sets AWS_REGION + AWS_DEFAULT_REGION"

    def get(self) -> str | None:
        return os.environ.get("AWS_REGION")

    def set(self, value: str) -> None:
        os.environ["AWS_REGION"] = value
        os.environ["AWS_DEFAULT_REGION"] = value

    @property
    def value_completer(self) -> Completer:
        return ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1"])

var_registry.register(AwsRegionVar())
```

#### Recipe Integration

Variable recipes follow the same `enable()` pattern as command completion recipes:

```python
# recipes/aws_vars.py
def _enable():
    var_registry.register(MultiEnvVar("aws_region", ["AWS_REGION", "AWS_DEFAULT_REGION"], ...))
    var_registry.register(EnvVar("aws_profile", "AWS_PROFILE", CallbackCompleter(list_profiles)))
```

```python
# ~/.cshell2/config.py
from cshell2.recipes import enable
enable("aws_vars")
```

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
    value: str              # the completion text (inserted into buffer)
    display: str = ""       # optional display label (shown in menu; defaults to value)
    description: str = ""   # optional description (shown beside completion)
    multi_select: bool = False   # True → opens InlineMultiPicker instead of InlinePicker
    combinable: bool = False     # True for single-char flags that can be merged (-a -l → -al)
    arg_hint: str = ""           # non-empty when flag requires a following argument (e.g. "N")
    is_arg_hint: bool = False    # True when this completion IS the hint for a preceding flag's value
```

#### Built-in Completers

```python
class FileCompleter(Completer): ...       # filesystem paths (files + dirs)
class DirCompleter(Completer): ...        # directory paths only
class CommandNameCompleter(Completer): ... # registered + system commands
class ChoiceCompleter(Completer):          # static list of choices
    def __init__(self, choices: list[str]): ...
class CallbackCompleter(Completer):        # dynamic list from a function
    def __init__(self, func: Callable[[], list[str]]): ...
class OptionsCompleter(Completer):         # flags with optional arg-hints and multi-select TUI
    def __init__(self, options: dict[str, str],
                 args: dict[str, str | tuple[str, Completer]] | None = None): ...
class ConditionalCompleter(Completer):     # pick sub-completer based on preceding args
    def __init__(self, mapping: dict[tuple, Completer]): ...
```

#### Per-Argument Completer Binding

Python commands declare positionals and flags via a single `params=[arg(...)]` list. Each `arg()` configures argparse (validation, defaults, action) **and** TAB completion in one place — `completer=` on a positional drives completion at that position; `completer=` on a value-taking flag drives completion of the value typed after the flag. The registry derives the underlying `{arg_index: Completer, None: OptionsCompleter}` dict automatically.

```python
@registry.command(
    name="ssh_instance",
    help="SSH into an EC2 instance.",
    params=[
        arg("account", choices=["account-A", "account-B"]),
        arg("region",  completer=RegionCompleter()),
        arg("instance_id", completer=EC2InstanceCompleter()),  # may inspect ctx.args[0]/[1]
        arg("-v", "--verbose", action="store_true", help="verbose"),
        arg("-p", "--port",    type=int, metavar="PORT",
                               help="port number"),
    ],
)
def ssh_instance(account, region, instance_id, verbose=False, port=22): ...
```

External completers for system commands take the underlying `{None: ..., N: ...}` dict directly:

```python
registry.register_external_completers("git", {
    None: OptionsCompleter({"-v": "verbose", "--no-pager": "no pager"}),
    0: ChoiceCompleter(["commit", "push", "pull", ...]),
})
```

#### OptionsCompleter — Multi-Select Flag Picker

When all completions have `multi_select=True` (returned by `OptionsCompleter`), pressing TAB opens `InlineMultiPicker` instead of `InlinePicker`. The user can:
- Navigate with arrows / `Ctrl+P` / `Ctrl+N`
- **Space** to toggle a flag's checked state
- **Enter** to confirm (checked items, or the highlighted item if nothing is checked)
- Jump to a flag by typing its first letter

Short boolean flags are automatically merged: selecting `-a` and `-l` inserts `-al`. Flags with `arg_hint` are inserted individually with a space, then followed by either a picker (if a value completer is registered via the `args` dict) or an inline hint prompting the user to type the value.

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
        # Fall back to longest matching prefix
        for length in range(len(ctx.args), 0, -1):
            partial_key = tuple(ctx.args[:length])
            if partial_key in self.mapping:
                return self.mapping[partial_key].complete(ctx)
        return []
```

### context.py — Context Switch

Contexts represent an environment (e.g., AWS account + region, k8s cluster). Each context stores:
- `variables: dict[str, str]` — exported to `os.environ` on activation
- `cwd: str` — saved and restored on switch
- `process_slot: ProcessSlot | None` — optional running subprocess for multiplexing
- `state: ContextState` — `IDLE`, `RUNNING`, or `EXITED` (derived from `process_slot`)

```python
class ContextManager:
    contexts: dict[str, Context]   # all known contexts by name
    current_name: str | None       # which context is active
    stack: list[str]               # push/pop stack (stores names)

    def create(self, name: str, variables: dict | None = None) -> Context: ...
    def switch(self, name: str): ...           # set current to any existing context
    def push(self, name: str): ...             # save current to stack, switch to name
    def pop(self) -> Context | None: ...       # switch back to previous on stack
    def current(self) -> Context | None: ...
    def list_contexts(self) -> list[str]: ...  # in display order (current first)
    def remove(self, name: str): ...
    def set_variable(self, key, value): ...    # set on current context + os.environ
    def unset_variable(self, key): ...         # remove from current context + os.environ
    def get_variable(self, key) -> str | None: ...
```

Context switching (shell commands):
```
cshell2> context push prod
Pushed context 'prod'
[prod] cshell2> var ACCOUNT=123456 REGION=us-east-1
[prod] cshell2> context push staging
Pushed context 'staging'
[staging] cshell2> var ACCOUNT=789012 REGION=us-west-2
[staging] cshell2> context pop
Popped 'staging', now in 'prod'
[prod] cshell2> context switch staging   # switch directly, prod still on stack
[staging] cshell2> context list          # show all: staging*, prod
[staging] cshell2> context kill prod     # send SIGTERM to running process in 'prod'
```

Completers can use `ctx.shell_context` to adapt:
```python
class EC2InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("account")
        region = ctx.args[1] if len(ctx.args) > 1 else ctx.shell_context.get_variable("region")
        ...
```

### lineedit.py — Line Editor

DIY raw-mode line editor. No prompt_toolkit or readline.

- `LineEditor.prompt()` — read one line; returns the line string, `SWITCH_SENTINEL` on `Ctrl+]`, raises `EOFError` (Ctrl+D on empty) or `KeyboardInterrupt` (Ctrl+C)
- Key bindings: `Ctrl+A/E`, `Ctrl+B/F`, `Alt+B/F`, `Ctrl+W`, `Ctrl+K`, `Ctrl+U`, `Ctrl+L`, arrow keys, `Ctrl+P/N`, `Ctrl+R`
- TAB opens an `InlinePicker` (or `InlineMultiPicker` for flags); typing narrows the list; TAB inside the picker extends the common prefix; Backspace can close the picker
- History search (`Ctrl+R`) opens a filterable picker over all history entries
- Multi-line wrapping is tracked so `_redraw()` correctly repositions the cursor after wraps
- VSCode integrated terminal detection: skips reflow-based repositioning, falls back to explicit clear+redraw on resize (`TERM_PROGRAM=vscode`)

### tui.py — Inline TUI Widgets

No alternate screen; all rendering anchored with DECSC/DECRC (`ESC 7` / `ESC 8`). Cancels on SIGWINCH.

- **`InlinePicker`** — single-select list rendered inline below the current line. Supports narrowing by typing, TAB-extend common prefix, scrollbar, optional `meta_fn` for right-aligned labels.
- **`InlineMultiPicker`** — multi-select list with Space to toggle checkboxes. Jump-to by typing a letter. Returns checked items (or highlighted item if nothing checked).
- **`InlineArgPrompt`** — single-line text prompt for a flag's argument. Shows an optional description line above.

### process.py — PTY Process Slots

`ProcessSlot` manages a single PTY-backed subprocess with output buffering, enabling context multiplexing.

- `start(argv, env, cwd)` — fork + exec in a new PTY; spawns a reader thread
- `activate() / deactivate()` — controls whether output is written to stdout
- `replay_buffer()` — flush buffered output when switching back to a context
- `write_stdin(data)` — forward raw bytes to the subprocess's PTY
- `resize(rows, cols)` — update PTY window size (sends SIGWINCH to child process group)
- `suspend_terminal_modes() / restore_terminal_modes()` — generate escape sequences to undo/redo DEC private modes (alt screen, mouse, app cursor keys) tracked across switches
- `kill()` — send SIGTERM

### prompt.py — Prompt Function

```python
def set_prompt(func: Callable[[ContextManager], str] | None) -> None: ...
def get_prompt_func() -> Callable[[ContextManager], str]: ...
```

Default prompt shows: `[context] path/cwd HH:MM:SS [bg:N]>` with ANSI colors. The `[context]` prefix is omitted when the context name is `"default"`. `[bg:N]` appears when N other contexts have live processes.

### recipes/ — Completion Recipes for External Commands

Opt-in completion recipes for system commands. Enable in `~/.cshell2/config.py`:

```python
from cshell2.recipes import enable
enable("make", "git", "docker", "ssh", "kill", "tail", "ls", "grep", "find", "du", "df", "aws")
```

Available built-in recipes: `aws`, `df`, `docker`, `du`, `find`, `git`, `grep`, `kill`, `ls`, `make`, `ssh`, `tail`.

Each recipe calls `registry.register_external_completers(name, {...})` with an `OptionsCompleter` under `None` and positional completers as needed.

#### User-Defined Recipes

`enable()` searches `recipe_search_path` (a `list[Path]`) when no built-in recipe matches. The default list contains only `~/.cshell2/recipes/`; call `add_recipe_path()` to append more directories. The call site in `config.py` is unchanged.

Lookup order for every `enable()` call:

1. Built-in package (`cshell2.recipes.<name>`) — always highest priority.
2. Each directory in `recipe_search_path` in order — first match wins.
3. `ImportError` with the searched directories listed if nothing is found.

A user recipe file must define a `register()` function with the same shape as built-in recipes:

```python
# ~/.cshell2/recipes/my_tool.py
from cshell2.commands import registry
from cshell2.completion import OptionsCompleter, ChoiceCompleter, CallbackCompleter

def register():
    registry.register_external_completers("my-tool", {
        None: OptionsCompleter(
            {"-v": "verbose", "--dry-run": "don't apply changes"},
        ),
        0: ChoiceCompleter(["deploy", "rollback", "status"]),
        1: CallbackCompleter(lambda: _list_targets()),
    })

def _list_targets():
    return ["web", "worker", "scheduler"]
```

```python
# ~/.cshell2/config.py
from cshell2.recipes import add_recipe_path, enable

add_recipe_path("/team/shared/recipes")  # optional extra directory
enable("git")          # built-in
enable("my_tool")      # found in ~/.cshell2/recipes/ or /team/shared/recipes/
```

`recipe_search_path` is a plain `list[Path]` and can be read or manipulated directly when finer control is needed.

### User Config (~/.cshell2/config.py)

Users define custom commands and completers here. Loaded at shell startup; reloadable with the `reload` command.

```python
# ~/.cshell2/config.py
from cshell2.commands import registry, arg
from cshell2.completion import Completer, Completion, ChoiceCompleter
from cshell2.recipes import enable

# Enable recipes for system commands
enable("make", "git")

class MyInstanceCompleter(Completer):
    def complete(self, ctx):
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("account")
        # ... fetch and return completions
        return [Completion(value="i-abc123", description="web-server-1")]

@registry.command(
    name="connect",
    help="SSH into an EC2 instance.",
    params=[
        arg("account", choices=["prod", "staging"]),
        arg("region",  choices=["us-east-1", "us-west-2", "eu-west-1"]),
        arg("instance_id", completer=MyInstanceCompleter()),
    ],
)
def connect(account, region, instance_id):
    import os
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
│       ├── __init__.py         # exports set_prompt
│       ├── __main__.py         # entry point
│       ├── shell.py            # main loop, command dispatch, pipeline execution
│       ├── commands.py         # command registry, @command decorator
│       ├── variables.py        # Var ABC, VarRegistry, EnvVar, VarCompleter
│       ├── completion.py       # Completer ABC, CompletionContext, built-in completers
│       ├── context.py          # Context, ContextManager, ContextState
│       ├── history.py          # history storage and search
│       ├── lineedit.py         # DIY raw-mode line editor, History, TAB completion glue
│       ├── parsing.py          # line tokenization, quote handling, var expansion
│       ├── pipeline.py         # quote-aware operator parser: parse_line(), expand_globs()
│       ├── process.py          # PTY subprocess slots, output buffering, terminal-mode tracking
│       ├── prompt.py           # set_prompt / get_prompt_func / default_prompt
│       ├── tui.py              # InlinePicker, InlineMultiPicker, InlineArgPrompt
│       └── recipes/
│           ├── __init__.py     # enable(*names) helper
│           ├── aws.py
│           ├── df.py
│           ├── docker.py
│           ├── du.py
│           ├── find.py
│           ├── git.py
│           ├── grep.py
│           ├── kill.py
│           ├── ls.py
│           ├── make.py
│           ├── ssh.py
│           └── tail.py
└── tests/
    ├── test_commands.py
    ├── test_completion.py
    ├── test_context.py
    ├── test_parsing.py
    ├── test_pipeline.py
    ├── test_process.py
    ├── test_recipes.py
    ├── test_shell_continuation.py
    └── test_variables.py

~/.cshell2/
├── config.py           # user configuration (commands, completers, recipes)
├── history             # persistent command history
└── recipes/            # user-defined recipes (loaded by enable("<name>"))
    └── <name>.py       # must define register()
```

## Shell Operator Support

### Implemented

**Tier 1 — Core (share a single pipeline parser):** ✅ all done
- Pipe `|` — `ls | grep py`
- Stdout redirect `>` `>>` — `make > build.log`
- Stdin redirect `<` — `sort < input.txt`
- Sequencing `;` `&&` `||` — `make && ./run`

**Tier 2 — High value, independent:**
- Glob expansion `*` `?` `**` ✅ — `expand_globs` with `recursive=True` for `**`
- Stderr redirect `2>` `2>>` `2>&1` ✅
- Backslash line continuation `\` ✅ — handled in `shell.py` before execution; continuation lines collected with `"> "` prompt; full joined command stored as one history entry
- Command substitution `$(…)` ❌ — not yet implemented

**Tier 3 — Nice to have:** ❌ none yet
- Background `&` (maps to auto context creation)
- Process substitution `<(cmd)`
- Here-documents `<<EOF`

### Implementation design

Parse order (all quote-aware, implemented in `pipeline.py` and `shell.py`):

```
raw input (one or more physical lines joined in shell.py)
 └─ backslash-continuation joining (shell.py — before any parsing)
     └─ split on ;          → list of statements
         └─ split on && ||  → conditional chain
             └─ split on |  → list of pipeline stages
                 └─ each stage: extract redirections (>, >>, <, 2>, 2>&1)
                     └─ remaining text: expand $VAR, tokenize, glob
```

Two execution modes in `shell.py`:

| Situation | Execution path |
|-----------|---------------|
| Standalone command (no pipe, no redirect) | PTY via `ProcessSlot` |
| Command in a pipeline | `subprocess.Popen` with plain fds |
| Command with redirect (no pipe) | `subprocess.run` with file fds |

Python registered commands (`@registry.command`) in a pipeline or with redirects have their `sys.stdout`/`sys.stdin`/`sys.stderr` temporarily replaced.

## Key Design Decisions

1. **DIY raw-mode line editor** — `lineedit.py` drives the terminal directly with `termios`/`tty`/`select`. This avoids external dependencies, keeps the codebase self-contained, and gives full control over the completion UI and resize handling.

2. **Completer receives full context** — the `CompletionContext` dataclass carries all parsed state so completers can make decisions based on command name, preceding args, and shell context without global state.

3. **Dict-based positional completers with `None` key for options** — `{arg_index: Completer}` for positional args; `{None: OptionsCompleter(...)}` for flags. A completer at position N can inspect `ctx.args[:N]` to see what was already chosen.

4. **Context as a stack with env+cwd isolation** — push/pop semantics let you temporarily enter a context and return. On every switch, context variables are unapplied from `os.environ` then the new context's variables are applied; CWD is saved and restored.

5. **PTY process multiplexing** — each context can hold a `ProcessSlot` with a live subprocess. `Ctrl+]` switches between contexts without killing the running process. The slot buffers output while inactive and replays it on return.

6. **Config as Python** — `config.py` is just Python that imports cshell2 APIs. No DSL to learn; full language power for defining completers with caching, API calls, etc. The `reload` command reloads the config without restarting the shell.

7. **System command fallback** — anything not registered as a Python command is passed to the system shell via PTY, so cshell2 is a drop-in replacement for daily use.

8. **Python-backed variables mirror the command registry pattern** — `Var` subclasses handle `get`/`set` logic; a single logical name (e.g. `aws_region`) can drive multiple `os.environ` keys or arbitrary side effects. The `var` command dispatches through `VarRegistry` before falling back to plain env writes, so the shell surface (`var KEY=VALUE`) is unchanged. `VarCompleter` handles `=`-split completion locally without touching the global tokenizer.
