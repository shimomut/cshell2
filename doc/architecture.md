# Architecture Overview

## System Diagram

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

## Module Responsibilities

### shell.py — Main Shell Loop

Entry point and orchestrator. Owns the REPL cycle: read input, parse, dispatch, repeat.

- Uses a DIY raw-mode line editor (`lineedit.py`) — no external dependencies
- Registers built-in commands: `cd`, `exit`, `reload`, `var`, `unset`, `help`, `context`
- Loads user configuration at startup; `reload` re-loads without restarting the shell
- Falls back to PTY subprocess (`process.py`) for unrecognized commands
- Executes pipelines (`|`), sequences (`;`, `&&`, `||`), and redirections (`>`, `>>`, `<`, `2>`, `2>&1`)
- Handles `Ctrl+]` context-switch picker (shows all contexts + running process names)

### commands.py — Command Registry

Provides the `CommandRegistry` class and a global `registry` singleton.

- `@registry.command()` decorator for registering Python functions as commands
- `registry.register()` for imperative registration
- `registry.register_external_completers(name, completers)` for attaching completers to system commands (e.g. `git`, `docker`) without wrapping them as Python commands
- `registry.mark_builtins()` / `registry.clear_user_commands()` for hot-reload support
- Each `Command` holds: name, callable, per-argument completers dict, help text
- Help text is automatically extracted from the function's docstring

### completion.py — Completion Engine

Defines the `Completer` protocol, `CompletionContext`, and built-in completers.

- `CompletionContext` carries full parse state to every completer
- `Completer` ABC with `complete()` and optional `should_activate()` guard
- Built-in completers: `FileCompleter`, `DirCompleter`, `CommandNameCompleter`, `ChoiceCompleter`, `CallbackCompleter`, `OptionsCompleter`, `ConditionalCompleter`
- `OptionsCompleter` supports multi-select flag TUI, flag arg-hints, value completers, and flag deduplication

### context.py — Context Manager

Manages named environments with variables, working directories, and optional running processes.

- `Context` dataclass: name, variables dict, saved cwd, optional `ProcessSlot`, `state` property (`IDLE`/`RUNNING`/`EXITED`)
- `ContextManager`: named collection with current pointer and push/pop stack
- On switch: saves current cwd, restores target cwd, swaps environment variables
- Environment variable backup/restore to avoid leaking between contexts
- `set_variable` / `unset_variable` update both the current context and `os.environ`

### lineedit.py — Line Editor

DIY raw-mode line editor. No prompt_toolkit or readline.

- `LineEditor.prompt()` — reads one line in raw terminal mode
- Full key binding suite: `Ctrl+A/E/B/F/W/K/U/L`, `Alt+B/F`, arrows, `Ctrl+P/N`, `Ctrl+R`, `Ctrl+]`
- TAB opens `InlinePicker` (or `InlineMultiPicker` for flags); supports narrowing by typing, TAB-extend, backspace-to-close
- `Ctrl+R` opens a filterable history picker
- Multi-line wrap tracking for correct cursor repositioning
- VSCode integrated terminal detection for resize handling (see `doc/terminal-resize.md`)

### tui.py — Inline TUI Widgets

Inline-rendered widgets anchored with DECSC/DECRC (no alternate screen). Cancel on SIGWINCH.

- `InlinePicker` — single-select list; supports narrowing by typing, scrollbar, `meta_fn` labels
- `InlineMultiPicker` — multi-select with Space; jump-to by letter; returns checked items (or highlighted)
- `InlineArgPrompt` — single-line text prompt for a flag's argument value

### process.py — PTY Process Slots

`ProcessSlot` manages a PTY-backed subprocess with output buffering for context multiplexing.

- `start()` forks a child in a new PTY; a reader thread streams output to `OutputBuffer`
- `activate()` / `deactivate()` route buffered output to stdout or hold it
- `replay_buffer()` flushes held output when switching back to a context
- `resize()` updates PTY window size and delivers SIGWINCH to the child process group
- `suspend_terminal_modes()` / `restore_terminal_modes()` generate escape sequences to undo/redo DEC private modes (alt screen, mouse, app cursor keys) across switches

### prompt.py — Prompt Function

Provides `set_prompt()` / `get_prompt_func()` for customizable prompt generation.

Default prompt: `[context] path/cwd HH:MM:SS [bg:N]>` (ANSI colors). The `[context]` prefix is omitted when the context name is `"default"`. `[bg:N]` appears when N other contexts have live processes.

### recipes/ — Completion Recipes

Opt-in completion recipes for system commands. Each recipe registers completers via `registry.register_external_completers()`. Enable in config:

```python
from cshell2.recipes import enable
enable("git", "docker", "make", "ssh", "kill", "ls", "grep", "find", "du", "df", "tail", "aws")
```

### parsing.py — Line Tokenization

Splits raw input into tokens respecting quoting rules.

- `split_for_completion(line)` returns `(tokens, prefix)` for the completion engine
- `expand_vars(line)` expands `$VAR` and `${VAR}`, leaving single-quoted regions unexpanded
- `tokenize(line)` wraps `shlex.split` with graceful recovery for unclosed quotes

### pipeline.py — Shell Operator Parser

Quote-aware parser for the full operator set.

- `parse_line(line)` returns a `Sequence` of `Pipeline` objects, each containing `Stage` objects with extracted `Redirect` lists
- `expand_globs(tokens)` expands `*`, `?`, `[`, and `**` patterns

## Data Flow

### Command Execution

```
User input → expand_vars() → parse_line() → Sequence of Pipelines
  → For each Pipeline:
      → Single stage, no redirect: PTY via ProcessSlot
      → Multiple stages (pipe): subprocess.Popen with OS pipes
      → Single stage with redirect: subprocess.run with file fds
  → Python commands have sys.stdout/stdin/stderr temporarily replaced
```

### Tab Completion

```
User presses TAB
  → LineEditor._complete()
    → _get_completions(line_before_cursor)
      → _split_on_operators() → isolate current pipeline stage
      → split_for_completion(stage) → (tokens, prefix)
      → No tokens? → CommandNameCompleter
      → Has tokens?
          → Look up command (registered or external completers)
          → Check completers[None] for OptionsCompleter (if prefix starts with "-")
          → Check completers[arg_index] for positional completer
          → No completer registered? → FileCompleter fallback
    → Single completion → _apply() directly
    → All multi_select completions → InlineMultiPicker
    → Otherwise → InlinePicker (narrows as user types)
```

### Context Switch

```
Ctrl+] pressed (or "context switch <name>")
  → _save_current(): snapshot cwd into current context
  → _unapply_env(): restore os.environ from backup
  → _activate(name):
      → os.chdir(target.cwd)
      → _apply_env(target): export target.variables, backup originals
  → If target context has live ProcessSlot: resume forwarding mode
```

## File Layout

```
cshell2/
├── CLAUDE.md               # Development instructions
├── README.md               # End-user documentation
├── pyproject.toml          # Package metadata, dependencies
├── doc/                    # Technical design documents
│   ├── architecture.md
│   ├── completion.md
│   ├── context.md
│   ├── recipes.md
│   └── terminal-resize.md
├── src/
│   └── cshell2/
│       ├── __init__.py         # exports set_prompt
│       ├── __main__.py         # entry point (calls Shell().run())
│       ├── shell.py            # main loop, command dispatch, pipeline execution
│       ├── commands.py         # command registry, @command decorator
│       ├── completion.py       # Completer ABC, CompletionContext, built-in completers
│       ├── context.py          # Context, ContextManager, ContextState
│       ├── history.py          # history storage and search
│       ├── lineedit.py         # DIY raw-mode line editor, TAB completion glue
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
    └── test_parsing.py
```

## Design Decisions

1. **DIY raw-mode line editor** — `lineedit.py` drives the terminal directly with `termios`/`tty`/`select`. This avoids external dependencies, keeps the codebase self-contained, and gives full control over the completion UI and resize handling.

2. **Completer receives full context** — `CompletionContext` carries all parsed state so completers make decisions based on command name, preceding args, and shell context without global state.

3. **Dict-based positional completers with `None` key for options** — `{arg_index: Completer}` for positional args; `{None: OptionsCompleter(...)}` for flags at any position. A completer at position N inspects `ctx.args[:N]` to see prior selections.

4. **Config as Python** — `~/.cshell2/config.py` is plain Python importing cshell2 APIs. No DSL to learn; full language power for defining completers with caching, API calls, conditional logic. `reload` applies changes without restarting.

5. **PTY process multiplexing** — each context can hold a `ProcessSlot` with a live subprocess. `Ctrl+]` switches between contexts without killing the running process; the slot buffers output while inactive and replays it on return.

6. **Context variables as env vars** — switching contexts exports variables to `os.environ` and backs up originals. This means subprocesses (system commands) automatically inherit context variables without cshell2-specific wiring.

7. **System command fallback** — unregistered commands pass through to a PTY subprocess, making cshell2 a drop-in replacement shell for daily use.
