# cshell2

A lightweight but powerful terminal shell environment implemented in Python.

## Before designing features or fixes — read these first

When designing a non-trivial new feature or working out a bug fix, always
consult these two documents *before* proposing a solution:

- [doc/limitations.md](doc/limitations.md) — known limitations of existing
  features. A "small" fix may already be a known gap with broader scope;
  one careful change can often resolve several entries at once.
- [doc/enhancements.md](doc/enhancements.md) — enhancement ideas and
  in-flight design drafts. A new feature may overlap with a planned one,
  or a bug fix may unlock part of an existing draft.

Cross-checking both lets you spot opportunities to solve multiple problems
together instead of stacking point fixes. When you ship something that
touches an entry in either doc, update or remove that entry in the same
change.

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
│  ├── CobraCompleter — drives <cmd> __complete      │
│  ├── ArgcompleteCompleter — drives argcomplete IPC │
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
│  Decorators (decorators/)                           │
│  ├── @name [flags] body — wrap pipeline at runtime │
│  └── Built-ins: @watch, @time, @retry, @quiet, @bg │
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
- Supports `Ctrl+R` history search (via inline picker) — searches the
  **global** history (every command from every context)
- Supports `Ctrl+]` to open an inline context-switch picker
- Maintains a global command history in `~/.cshell2/history` (every executed
  command, across all contexts). **Up/Down navigation is scoped per context**
  (each `Context` carries an in-memory `history` list); the global file backs
  `Ctrl+R` and seeds the `default` context's Up/Down list at startup. A newly
  created context snapshots its parent's Up/Down list, then they diverge.
  Per-context lists are in-memory only — not persisted across restarts.
- Runs external commands in PTY-backed subprocess slots (`process.py`)
- Executes pipelines (`|`), sequences (`;`, `&&`, `||`), and redirections (`>`, `>>`, `<`, `2>`, `2>&1`)

**Built-in commands:** `cd`, `exit`, `reload`, `var`, `unset`, `help`, `context`

**Ctrl+] context switching:** The user can press `Ctrl+]` at the shell prompt (or during a running process) to open a TUI picker listing all contexts. Selecting a context with a live process resumes it immediately. While the picker is open, the focused context's last few lines of buffered output are previewed below the list, and the following action keys mutate the context list in place: `Ctrl+N` creates a new context (inheriting the current context's variables), `Ctrl+D` deletes the focused context (including the current one — the manager picks the next current automatically), `Ctrl+R` renames the focused context. Action keys refuse to delete a context with a live process or to leave fewer than one context. The shell tracks processes across context switches via `ProcessSlot` (see `process.py`).

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
- `command(name, *, help=None, params=None, delegate=None, options_completer=None)` — register a Python function (with handler) or an external recipe (no handler attached). `params=[arg(...)]` declares positionals and flags; the registry derives both an argparse parser and the per-position completer dict from the same list. `delegate=Completer` installs a single completer at every slot (used when an external tool drives its own completion protocol). `options_completer=OptionsCompleter` overrides the auto-built flag completer when a custom subclass is needed.
- `register(cmd: Command)` — register a pre-built `Command` object (mirrors `var_registry.register(var_object)`)
- `mark_builtins()` — snapshot current commands as builtins (not removed on `reload`)
- `clear_user_commands()` — remove non-builtin commands and aliases

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

**`$NAME` / `${NAME}` expansion is symmetric with assignment.** `parsing.expand_vars` checks `var_registry` first, then `os.environ`, so a Python-backed variable can be read on the command line just like an env var:

```
var aws_region=us-west-2
aws ec2 describe-instances --region $aws_region   → expands via AwsRegionVar.get()
echo $AWS_REGION                                   → also us-west-2 (set() wrote both keys)
```

The same precedence applies to bare `NAME=VALUE` assignment (e.g. `aws_region=ap-south-1`). Net effect: Python-backed vars and OS env vars are interchangeable on both the read and write sides of the shell surface.

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

#### Registering Vars from `~/.cshell2/config.py`

`Var` subclasses are plain Python — register them directly from the user
config (or any module the config imports). `EnvVar(name, env_var,
completer=...)` is the convenience subclass for a single-key passthrough;
when a logical name needs to drive multiple `os.environ` keys, subclass
`Var` directly:

```python
# ~/.cshell2/config.py
from cshell2.completion import CallbackCompleter, ChoiceCompleter
from cshell2 import Var, EnvVar, var_registry

class AwsRegionVar(Var):
    name = "aws_region"
    description = "AWS region — sets AWS_REGION + AWS_DEFAULT_REGION"

    def get(self):
        return os.environ.get("AWS_REGION")

    def set(self, value):
        os.environ["AWS_REGION"] = value
        os.environ["AWS_DEFAULT_REGION"] = value

    @property
    def value_completer(self):
        return ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1"])

var_registry.register(AwsRegionVar())
var_registry.register(
    EnvVar("aws_profile", "AWS_PROFILE",
           completer=CallbackCompleter(list_profiles))
)
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

External recipes use the same `registry.command()` API as Python commands — they just omit the handler.  `nargs="*"` / `"+"` on a positional makes that completer serve every trailing slot, replacing the old per-index dict spam:

```python
registry.command(
    "git",
    help="distributed version control",
    params=[
        arg("subcommand", choices=["commit", "push", "pull", ...]),
        arg("path", nargs="*", help="repository path", completer=FileCompleter()),
        arg("-v", "--verbose", action="store_true", help="verbose"),
        arg("--no-pager", action="store_true", help="no pager"),
    ],
)
```

Two escape hatches on `registry.command()` cover the cases where flags or positional dispatch can't be expressed via `params` alone:

* `delegate=Completer` — install a single completer at **every** slot (flags + every positional index).  Used when an external tool ships its own completion protocol that decides per-call what to return (e.g. `aws_completer`).
* `options_completer=OptionsCompleter` — override the auto-built flag completer at the `None` slot.  Reserved for cases that need a custom :class:`OptionsCompleter` subclass; no built-in recipe currently uses it.

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
- `history: list[str]` — per-context Up/Down command history (in-memory). Seeded
  from the global history file for `default`, or snapshotted from the parent at
  `create()` time; `Ctrl+R` ignores this and searches the global store instead

```python
class ContextManager:
    contexts: dict[str, Context]   # all known contexts by name
    current_name: str | None       # which context is active
    stack: list[str]               # push/pop stack (stores names)

    def create(self, name: str, variables: dict | None = None,
               history: list[str] | None = None) -> Context: ...
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
- All raw-mode entry and key reading goes through `terminal.py` (not `termios`/`tty`/`select` directly), so the editor runs unchanged on POSIX and native Windows.

### terminal.py — Cross-Platform Terminal Layer

The single place that touches OS-specific terminal APIs. `lineedit.py`, `tui.py`, and the POSIX forwarding loops in `shell.py` drive the terminal through this module so the rendering/key-dispatch code stays platform-agnostic.

- `init()` — one-time setup. On Windows: enables VT output processing (so the ANSI escapes the renderer emits are honoured) and disables Python's `\n`→`\r\n` translation on the std streams. No-op on POSIX.
- `get_mode(fd)` / `set_raw(fd)` / `restore_mode(fd, saved)` — enter/leave raw mode. POSIX: `termios`/`tty` (TCSADRAIN). Windows: toggles `DISABLE_NEWLINE_AUTO_RETURN` so a bare `\n` is a pure line-feed while rendering and reverts to auto-CR for cooked output.
- `read_key(fd)` — block for one *complete logical key* as bytes: a control byte, a full UTF-8 char (all continuation bytes), or a complete escape sequence (`b"\x1b[A"`). POSIX reads via `os.read`+`select`; Windows reads via `msvcrt`, translating the `\x00`/`\xe0` scan-code prefixes into the same ANSI sequences POSIX produces. Callers compare against fixed byte patterns and never re-read the stream.
- `wait_readable(fd, timeout)` — POSIX `select`; Windows polls `msvcrt.kbhit`.
- `install_resize_handler` / `restore_resize_handler` / `HAS_SIGWINCH` — SIGWINCH wiring on POSIX; no-ops on Windows (where the TUI widgets detect resize by polling `terminal_size()` between key reads and cancel, same as they did on SIGWINCH).

**Path separators.** The shell uses `/` as the canonical separator on every platform (like Git Bash / MSYS) — Windows file APIs and executables accept it natively. This keeps `\` free for its POSIX meaning (escaping, `\`-line-continuation), so a path can never be mistaken for a continuation (`cd C:/Users/` not `cd C:\Users\`). `os.path` helpers emit native `\` on Windows, so completer output is normalized with `completion._to_slash` and the prompt renders `/` too. Users type `/` for paths; `\` still escapes as usual.

**Platform support.** The full interactive shell — line editing, completion, all TUI pickers, history, pipelines, redirects, built-ins, and `Ctrl+]` context switching at the prompt — runs natively on both POSIX and Windows. The one POSIX-only piece is **PTY-backed multiplexing of a live external process** (`process.py`'s `ProcessSlot`, the `passthrough_run` PTY, and the `_enter_forwarding_mode` loops): backgrounding a *running* native program via `Ctrl+]` and resuming it. On Windows, external commands run on the real console with inherited stdio (`_execute_external_windows`, with a `cmd /c` fallback for `cmd` builtins like `dir`/`echo`), and Python `@registry.command`s run synchronously on the main thread. Reviving that subsystem on Windows would mean a ConPTY (`CreatePseudoConsole`) backend for `ProcessSlot`.

**Setting up a Windows dev environment** (Python, `make`, POSIX tools, and the `Makefile`'s `2>nul` vs. `2>/dev/null` gotcha) is covered in [doc/windows-setup.md](doc/windows-setup.md).

### tui.py — Inline TUI Widgets

No alternate screen; all rendering anchored with DECSC/DECRC (`ESC 7` / `ESC 8`). On POSIX a resize arrives via SIGWINCH; on Windows it is detected by polling `terminal.terminal_size()` between key reads. Either way the picker cancels (redrawing without an alt-screen is unreliable — the user presses TAB again).

- **`InlinePicker`** — single-select list rendered inline below the current line. Supports narrowing by typing, TAB-extend common prefix, scrollbar, optional `meta_fn` for right-aligned labels.
- **`InlineMultiPicker`** — multi-select list with Space to toggle checkboxes. Jump-to by typing a letter. Returns checked items (or highlighted item if nothing checked).
- **`InlineArgPrompt`** — single-line text prompt for a flag's argument. Shows an optional description line above.

### process.py — PTY Process Slots (POSIX only)

`ProcessSlot` manages a single PTY-backed subprocess with output buffering, enabling context multiplexing. It depends on `pty`/`fcntl`/`termios` and is never instantiated on Windows (the module still imports cleanly there — the Unix-only imports are guarded — but `_execute_external` takes the inherited-stdio path instead). See the Platform support note under `terminal.py`.

- `start(argv, env, cwd)` — fork + exec in a new PTY; spawns a reader thread
- `activate() / deactivate()` — controls whether output is written to stdout
- `replay_buffer()` — flush buffered output when switching back to a context
- `write_stdin(data)` — forward raw bytes to the subprocess's PTY
- `resize(rows, cols)` — update PTY window size (sends SIGWINCH to child process group)
- `suspend_terminal_modes() / restore_terminal_modes()` — generate escape sequences to undo/redo DEC private modes (alt screen, mouse, app cursor keys) tracked across switches
- `kill()` — send SIGTERM

### Spawning interactive subprocesses from Python commands

A Python `@registry.command` runs in a background thread inside a `PythonCommandSlot`. While it runs, the main thread holds stdin in raw mode and forwards bytes to the slot via `write_stdin`. If the command body calls `subprocess.run([...])` directly, the child inherits the real terminal stdin — and now the main thread *and* the subprocess are both calling `read()` on fd 0. Whoever wins each keystroke gets it; the other sees nothing. Symptoms: dropped keys, garbled input, Ctrl+] sometimes reaches the subprocess.

External commands typed at the prompt (e.g. plain `aws ssm start-session`) don't have this problem because they're routed through `ProcessSlot`, which gives them a dedicated PTY pair. The main thread is the *only* reader of real stdin; it copies bytes into the PTY master.

The fix for Python commands is the same shape: spawn the subprocess against a slot-owned PTY and let the existing forwarding loop do its job. Use `cshell2.passthrough_run`:

```python
from cshell2 import passthrough_run

@registry.command(name="my_ssm", ...)
def my_ssm():
    passthrough_run(["aws", "ssm", "start-session", "--target", target])
```

`passthrough_run` allocates a PTY on the enclosing `PythonCommandSlot`, starts the subprocess against the slave, and spawns a reader thread that copies output to stdout (or buffers it while the context is backgrounded). The main thread keeps reading real stdin in raw mode, intercepts Ctrl+] for context switching, and forwards every other byte to `slot.write_stdin` — which now writes to the PTY master. Ctrl+C is delivered to the subprocess (not the Python thread) while a passthrough subprocess is active. Window resizes propagate via `slot.resize()` → `TIOCSWINSZ` + `SIGWINCH` on the child's process group.

Outside a Python command thread (e.g. inside a synchronous handler that doesn't run on a slot), `passthrough_run` falls through to plain `subprocess.run`.

**Reading a line of input from the user.** `input()` from a Python command body has the same race as `subprocess.run` — the main thread is also reading stdin in raw mode, so most keystrokes are lost and Enter arrives as `\r` with no echo. Use `cshell2.passthrough_input(prompt)` instead: the slot signals the main loop to restore cooked terminal mode and stop reading stdin for the duration of the call, then takes it back. Built-in commands like `exit`'s "Exit anyway? [y/N]" confirmation use this. Outside a Python command thread, `passthrough_input` falls through to plain `input()`.

**When you don't need it.** Three cases that look like subprocesses but don't race for stdin:

1. **Non-interactive subprocesses** (`subprocess.run(..., capture_output=True)`, `$()` substitution, completer queries that shell out to `git`/`docker`/`aws`). The child doesn't read fd 0, so there's no race. Plain `subprocess.run` is fine.
2. **`subprocess.Popen` with explicit pipes/redirections** in pipeline stages. The shell already wires stdin/stdout to file descriptors that aren't the terminal, so the child never touches real stdin.
3. **`pexpect.popen_spawn.PopenSpawn`** (and any other library that drives the child via its own pipe). PopenSpawn passes `stdin=subprocess.PIPE` and writes via `sendline()`, so the child's stdin is owned entirely by the parent process — the user's keystrokes never reach it.

Rule of thumb: if a subprocess spawned from a Python command would, when run standalone in a terminal, read keystrokes from the user (SSH-like sessions, TUIs, MFA prompts, anything that calls `getpass`), wrap it with `passthrough_run`. Otherwise leave it as `subprocess.run`.

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
enable("make", "git", "ssh", "kill", "tail", "ls", "grep", "find", "du", "df", "aws")
```

Available built-in recipes: `aws`, `awsut`, `chmod`, `chown`, `cp`, `curl`, `df`, `du`, `find`, `git`, `grep`, `kill`, `ls`, `lsof`, `make`, `mv`, `ps`, `rm`, `rsync`, `scp`, `ssh`, `tail`, `tar`, `terraform`, `top`, `unzip`, `zip` (see the `Available recipes:` block in `src/cshell2/recipes/__init__.py` for descriptions). Use `enable("*")` to load all built-ins plus user recipes.

**Protocol fallbacks** — auto-activate after recipes, no `enable()` required:

- **`CobraCompleter`** drives `<cmd> __complete` for cobra-based CLIs (`docker`, `kubectl`, `helm`, `gh`, `argocd`, …). See `doc/cobra-fallback.md`.
- **`ArgcompleteCompleter`** drives the argcomplete protocol (env vars + fd 8) for Python CLIs marked with `# PYTHON_ARGCOMPLETE_OK` (`pipx`, `conda`, `pre-commit`, `tox`, `pdm`, `httpie`, …). See `doc/argcomplete-fallback.md`.

Each recipe calls `registry.command(name, help=..., params=[...])` (with no handler attached) to register completion + flag metadata for an external command.  The shell's dispatch path (`shell.py:_execute`) treats handler-less Commands as external recipes and falls through to the system-command path.

#### User-Defined Recipes

`enable()` searches `recipe_search_path` (a `list[Path]`) when no built-in recipe matches. The default list contains only `~/.cshell2/recipes/`; call `add_recipe_path()` to append more directories. The call site in `config.py` is unchanged.

Lookup order for every `enable()` call:

1. Built-in package (`cshell2.recipes.<name>`) — always highest priority.
2. Each directory in `recipe_search_path` in order — first match wins.
3. `ImportError` with the searched directories listed if nothing is found.

A user recipe file must define a `register()` function with the same shape as built-in recipes:

```python
# ~/.cshell2/recipes/my_tool.py
from cshell2.commands import arg, registry
from cshell2.completion import CallbackCompleter, ChoiceCompleter

def register():
    registry.command(
        "my-tool",
        help="my-tool — deploy/rollback/status helper",
        params=[
            arg("subcommand", choices=["deploy", "rollback", "status"]),
            arg("target", help="deploy target", completer=CallbackCompleter(_list_targets)),
            arg("-v", "--verbose", action="store_true", help="verbose"),
            arg("--dry-run", action="store_true", help="don't apply changes"),
        ],
    )

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

### decorators/ — Pipeline Decorators

A **decorator** is a token of the form `@name [flags]` at the start of a line that wraps the rest of the line as a pipeline and modifies how that pipeline is run. The leading `@` makes the syntax visually distinct from regular commands so parsing priority is unambiguous and the construct doesn't collide with POSIX command names.

```
@watch ls                              # bare single-command body
@watch -n 1 {df -h | grep abc}         # braced body required when operators appear
@watch --no-clear {tail -f log}
```

**Scope rule.** Any pipeline that contains `|`, `;`, `&&`, `||`, or a redirect must be enclosed in `{...}`. The single-command form is allowed without braces. Operators outside braces raise `DecoratorParseError` at parse time with a message pointing at the offending operator.

**Brace handling.** The brace-balancer in `pipeline.py::_find_matching_brace` shares a single scan with the existing quote/escape tracker and handles three things so braces inside the body don't terminate the scope:

1. Single-quoted regions: every char (including `}`) is literal.
2. Double-quoted regions: `"}"` is literal; `${...}` inside is still a balanced span.
3. `${name}` parameter expansion: matched as its own balanced `{...}` so the inner closing brace doesn't decrement the outer counter.
4. Backslash escapes: `\{` and `\}` are literal.

**Authoring a decorator:**

```python
from cshell2.commands import arg
from cshell2.decorators import registry as decorator_registry

@decorator_registry.decorator(
    name="watch",
    help="Repeatedly run a pipeline until interrupted.",
    params=[
        arg("-n", "--interval", type=float, default=2.0, metavar="SEC"),
        arg("--no-clear", action="store_true"),
    ],
)
def watch(pipeline, *, interval, no_clear):
    while True:
        if not no_clear and sys.stdout.isatty():
            sys.stdout.write("\x1b[2J\x1b[H")
        pipeline.run()
        time.sleep(interval)
```

The decorator function receives a `Pipeline` (the parsed AST of the wrapped body) and the parsed flag namespace as kwargs. `pipeline.run()` re-enters `Shell._execute_pipeline` so redirects, pipes, and Python-stage routing all work the same as at the top level.

**Built-in decorators:** `@watch`, `@time`, `@retry`, `@quiet`, `@bg` (each in its own `cshell2/decorators/<name>.py`).

**Loading:** `Shell._register_builtins` calls `enable_decorators("watch", "time", "retry", "quiet", "bg")` on construction. The `enable("*")` helper, search-path mechanism, and `add_decorator_path()` mirror `cshell2/recipes/`.

**`@bg` and slot infrastructure.** `@bg` runs its body on a `PipelineSlot` — a subclass of `PythonCommandSlot` whose work unit is a `Pipeline.run()` call instead of a single Python command. It registers itself as the new context's `process_slot`, so the run-loop's existing resume path (proxy buffering, `Ctrl+]` switching, `_compute_exit_code`) handles it without further wiring. The decorator-side hook is `set_background_runner()` in `cshell2.decorators` (parallel to `set_pipeline_executor`); `Shell.__init__` registers `_run_in_background` against it.

**Caveats inherited from in-process Python pipelines.** A decorator body is a Python command in everything but syntax, so the constraints from `doc/limitations.md` ("Python commands in pipelines — caveats of the in-process model") apply: nested `subprocess.run` writes to the real terminal unless given `stdout=sys.stdout`, pure-CPU loops can't be `Ctrl+C`-interrupted in a piped context, and `passthrough_run`/`passthrough_input` raise `RuntimeError` from a piped decorator.

See [doc/decorators.md](doc/decorators.md) for the full design rationale, IPython-magic precedent, parser/executor walkthrough, and resolved UX questions; remaining follow-ups (stacking, more built-ins, …) are in [doc/enhancements.md](doc/enhancements.md).

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

#### Custom Decorators

Custom decorators register the same way commands do — import
`cshell2.decorators.registry` and decorate a function. The function
receives the wrapped `Pipeline` as its first positional argument and the
parsed flag namespace as kwargs; call `pipeline.run()` to execute the
body. Return the int exit code (or let the return value of `pipeline.run()`
propagate).

```python
# ~/.cshell2/config.py
import sys
import time
from cshell2.commands import arg
from cshell2.decorators import registry as decorator_registry
from cshell2.pipeline import Pipeline

@decorator_registry.decorator(
    name="repeat",
    help="Run the pipeline N times, stopping early on the first failure.",
    params=[
        arg("-n", "--count", type=int, default=3, metavar="N",
            help="number of iterations (default 3)"),
        arg("--delay", type=float, default=0.0, metavar="SEC",
            help="seconds to sleep between iterations"),
    ],
)
def repeat(pipeline: Pipeline, *, count: int, delay: float) -> int:
    last = 0
    for i in range(1, count + 1):
        sys.stderr.write(f"@repeat: iteration {i}/{count}\n")
        last = pipeline.run()
        if last != 0:
            return last
        if delay > 0 and i < count:
            time.sleep(delay)
    return last
```

Usage at the prompt — the brace form is required when the body contains
pipeline operators:

```
cshell2> @repeat -n 5 --delay 1 ls
cshell2> @repeat -n 3 {make && ./run-tests}
```

For decorators shared across machines or teammates, drop a module under
`~/.cshell2/decorators/<name>.py` that defines `register()` (same shape
as the built-ins) and call `enable()` from `config.py`:

```python
# ~/.cshell2/config.py
from cshell2.decorators import add_decorator_path, enable as enable_decorators

add_decorator_path("/team/shared/decorators")   # optional extra directory
enable_decorators("repeat")                     # found in ~/.cshell2/decorators/
                                                # or /team/shared/decorators/
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
│       ├── completion_cache.py # TTL store for completer fetches; invalidated after every command
│       ├── context.py          # Context, ContextManager, ContextState
│       ├── history.py          # history storage and search
│       ├── lineedit.py         # DIY raw-mode line editor, History, TAB completion glue
│       ├── parsing.py          # line tokenization, quote handling, var expansion
│       ├── pipeline.py         # quote-aware operator parser: parse_line(), expand_globs(), decorator extraction, Pipeline.run()
│       ├── process.py          # PTY subprocess slots, output buffering, terminal-mode tracking
│       ├── prompt.py           # set_prompt / get_prompt_func / default_prompt
│       ├── tui.py              # InlinePicker, InlineMultiPicker, InlineArgPrompt
│       ├── recipes/
│       │   ├── __init__.py     # enable(*names) helper
│       │   ├── aws.py
│       │   ├── df.py
│       │   ├── du.py
│       │   ├── find.py
│       │   ├── git.py
│       │   ├── grep.py
│       │   ├── kill.py
│       │   ├── ls.py
│       │   ├── make.py
│       │   ├── ssh.py
│       │   └── tail.py
│       └── decorators/
│           ├── __init__.py     # Decorator/DecoratorRegistry/enable(*names)
│           ├── watch.py        # @watch built-in
│           ├── time.py         # @time built-in
│           ├── retry.py        # @retry built-in
│           ├── quiet.py        # @quiet built-in
│           └── bg.py           # @bg built-in
└── tests/
    ├── test_commands.py
    ├── test_completion.py
    ├── test_completion_cache.py
    ├── test_context.py
    ├── test_decorators.py
    ├── test_parsing.py
    ├── test_pipeline.py
    ├── test_process.py
    ├── test_recipes.py
    ├── test_shell_continuation.py
    └── test_variables.py

~/.cshell2/
├── config.py           # user configuration (commands, completers, recipes)
├── history             # persistent command history
├── recipes/            # user-defined recipes (loaded by enable("<name>"))
│   └── <name>.py       # must define register()
└── decorators/         # user-defined decorators (loaded by enable("<name>"))
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
| Standalone external command (no pipe, no redirect) | PTY via `ProcessSlot` |
| External command in a pipeline | `subprocess.Popen` with plain fds |
| External command with redirect (no pipe) | `subprocess.run` with file fds |
| Python `@registry.command` in a pipeline | worker thread per stage; thread-local `sys.stdin`/`sys.stdout`/`sys.stderr` rebound to pipe ends |
| Python `@registry.command` with redirect (no pipe) | runs synchronously on main thread; `sys.std*` swapped around `cmd.invoke()` |

The thread-local routing (`_ThreadLocalStdin` / `_ThreadLocalStdout` / `_ThreadLocalStderr` in `shell.py`) is what lets multiple Python pipeline stages run concurrently without trampling each other or the main thread's terminal. Caveats — most importantly that nested `subprocess` from inside a piped Python command bypasses the thread-local rebinding because it reads the real fd 1 — are documented in `doc/limitations.md` under "Python commands in pipelines — caveats of the in-process model."

## Key Design Decisions

1. **DIY raw-mode line editor** — `lineedit.py` drives the terminal directly with `termios`/`tty`/`select`. This avoids external dependencies, keeps the codebase self-contained, and gives full control over the completion UI and resize handling.

2. **Completer receives full context** — the `CompletionContext` dataclass carries all parsed state so completers can make decisions based on command name, preceding args, and shell context without global state.

3. **Dict-based positional completers with `None` key for options** — `{arg_index: Completer}` for positional args; `{None: OptionsCompleter(...)}` for flags. A completer at position N can inspect `ctx.args[:N]` to see what was already chosen.

4. **Context as a stack with env+cwd isolation** — push/pop semantics let you temporarily enter a context and return. On every switch, context variables are unapplied from `os.environ` then the new context's variables are applied; CWD is saved and restored.

5. **PTY process multiplexing** — each context can hold a `ProcessSlot` with a live subprocess. `Ctrl+]` switches between contexts without killing the running process. The slot buffers output while inactive and replays it on return.

6. **Config as Python** — `config.py` is just Python that imports cshell2 APIs. No DSL to learn; full language power for defining completers with caching, API calls, etc. The `reload` command reloads the config without restarting the shell.

7. **System command fallback** — anything not registered as a Python command is passed to the system shell via PTY, so cshell2 is a drop-in replacement for daily use.

8. **Python-backed variables mirror the command registry pattern** — `Var` subclasses handle `get`/`set` logic; a single logical name (e.g. `aws_region`) can drive multiple `os.environ` keys or arbitrary side effects. The `var` command and bare `NAME=VALUE` assignment dispatch through `VarRegistry` before falling back to plain env writes, and `$NAME` / `${NAME}` expansion does the same lookup in reverse — so registered Vars are read- and write-symmetric with `os.environ` and a Python-backed variable behaves transparently like an OS variable on the command line. `VarCompleter` handles `=`-split completion locally without touching the global tokenizer.

9. **One reader for real stdin** — when a Python command spawns an interactive subprocess, the child must not inherit fd 0 directly. The main forwarding thread is already reading stdin in raw mode; a second reader (the subprocess) splits keystrokes unpredictably between them. `passthrough_run` enforces the rule by allocating a slot-owned PTY for the child, so the chain stays `stdin → main → master → subprocess`. This is the same architecture `ProcessSlot` uses for external commands; `passthrough_run` extends it to subprocesses launched from inside a `PythonCommandSlot`.

10. **Decorators as a sigil-prefixed grammar, not a built-in command** — `@name [flags] body` is parsed *before* the normal pipeline grammar runs (`pipeline.py::_extract_decorator_prefix`), so the syntax is unambiguous to the parser and can never collide with a POSIX command name. Borrowed from IPython's magics (`%name args`); see [doc/decorators.md](doc/decorators.md). The `{...}` body delimiter is required when the wrapped pipeline contains operators, which makes the decorator's scope visible at a glance and side-steps the `watch -n 5 ls | grep abc` ambiguity that POSIX `watch` is famous for. `Pipeline.run()` lets a decorator body re-enter `Shell._execute_pipeline` so redirects/pipes/Python-stage routing all work the same as at the top level.

11. **TTL cache + command-boundary invalidation for completer fetches** — TAB completion runs the completer on every keystroke while the picker is open (see `lineedit.py::refresh_fn`). Completers that hit AWS APIs (e.g. `aws_completer`, `_HyperpodNodeIdCompleter`) would otherwise issue the same boto3 call four or five times for a single typed token. `completion_cache.py` provides `get_or_fetch(key, fn, ttl=60)` with a process-global store. Keys are tuples that include the active `(AWS_PROFILE, AWS_REGION)` via `aws_env_key()` so the cache doesn't bleed across profiles. `Shell._execute()` calls `completion_cache.invalidate_all()` after each pipeline finishes, so a freshly-mutated resource (e.g. after `awsut hyperpod scale`) is re-fetched on the next TAB — TTL handles the within-session repeats, the invalidation hook handles correctness across commands.
