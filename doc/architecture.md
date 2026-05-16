# Architecture Overview

## System Diagram

```
┌─────────────────────────────────────────────────────┐
│                    cshell2                           │
├─────────────────────────────────────────────────────┤
│  Shell Loop (shell.py)                              │
│  ├── Input handling (prompt_toolkit)                │
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
│  ├── Named context collection                      │
│  ├── Push/pop stack                                │
│  └── Environment variable management              │
├─────────────────────────────────────────────────────┤
│  User Config (~/.cshell2/config.py)                 │
│  ├── Custom command definitions                    │
│  └── Custom completer definitions                  │
└─────────────────────────────────────────────────────┘
```

## Module Responsibilities

### shell.py — Main Shell Loop

Entry point and orchestrator. Owns the REPL cycle: read input, parse, dispatch, repeat.

- Integrates with `prompt_toolkit` for terminal input (rich completion UI, history search, key bindings)
- Bridges cshell2's completion engine to prompt_toolkit via `ShellCompleter`
- Registers built-in commands (`cd`, `exit`, `help`, `context`)
- Loads user configuration at startup
- Falls back to system subprocess for unrecognized commands

### commands.py — Command Registry

Provides the `CommandRegistry` class and a global `registry` singleton.

- `@registry.command()` decorator for registering Python functions as commands
- `registry.register()` for imperative registration
- Each `Command` holds: name, callable, per-argument completers dict, help text
- Help text is automatically extracted from the function's docstring

### completion.py — Completion Engine

Defines the `Completer` protocol, `CompletionContext`, and built-in completers.

- `CompletionContext` carries full parse state to every completer
- `Completer` ABC with `complete()` and optional `should_activate()` guard
- Built-in completers: `FileCompleter`, `CommandNameCompleter`, `ChoiceCompleter`, `CallbackCompleter`, `ConditionalCompleter`

### context.py — Context Manager

Manages named environments with variables and working directories.

- `Context` dataclass: name, variables dict, saved cwd
- `ContextManager`: named collection with current pointer and stack
- On switch: saves current cwd, restores target cwd, swaps environment variables
- Environment variable backup/restore to avoid leaking between contexts

### parsing.py — Line Tokenization

Splits raw input into tokens respecting quoting rules.

- `split_for_completion(line)` returns `(tokens, prefix)` for the completion engine

## Data Flow

### Command Execution

```
User input → split_for_completion() → tokens
  → registry.get(tokens[0])
    → found: call cmd.func(*tokens[1:])
    → not found: subprocess.run([tokens[0]] + tokens[1:])
```

### Tab Completion

```
User presses TAB
  → prompt_toolkit calls ShellCompleter.get_completions()
    → split_for_completion(line) → (tokens, prefix)
    → No tokens? → CommandNameCompleter
    → Has tokens?
        → Look up command in registry
        → Check cmd.completers[arg_index]
            → Found completer → completer.complete(ctx)
            → No completer → FileCompleter fallback
    → Yield PTKCompletion objects
```

### Context Switch

```
context switch <name>
  → _save_current(): snapshot cwd into current context
  → _restore(target):
      → _unapply_env(): restore os.environ from backup
      → os.chdir(target.cwd)
      → _apply_env(target): export target.variables, backup originals
```

## File Layout

```
cshell2/
├── CLAUDE.md           # Development instructions
├── README.md           # End-user documentation
├── pyproject.toml      # Package metadata, dependencies
├── doc/                # Technical design documents
│   ├── architecture.md
│   ├── completion.md
│   └── context.md
├── src/
│   └── cshell2/
│       ├── __init__.py
│       ├── __main__.py       # entry point (calls Shell().run())
│       ├── shell.py          # main loop, input handling, builtins
│       ├── commands.py       # command registry, @command decorator
│       ├── completion.py     # Completer ABC, CompletionContext, built-ins
│       ├── context.py        # Context, ContextManager
│       ├── history.py        # history storage and search
│       └── parsing.py        # line tokenization, quote handling
└── tests/
    ├── test_commands.py
    ├── test_completion.py
    ├── test_context.py
    └── test_parsing.py
```

## Design Decisions

1. **prompt_toolkit over raw readline** — Rich completion menu with descriptions, async completion, better cross-platform support, multi-line editing. The shell bridges to prompt_toolkit via `ShellCompleter` adapter.

2. **Completer receives full context** — `CompletionContext` carries all parsed state so completers make decisions based on command name, preceding args, and shell context without global state.

3. **Dict-based positional completers** — `{arg_index: Completer}` gives each argument position independent logic. A completer at position N inspects `ctx.args[:N]` to see prior selections.

4. **Config as Python** — `~/.cshell2/config.py` is plain Python importing cshell2 APIs. No DSL to learn; full language power for defining completers with caching, API calls, conditional logic.

5. **System command fallback** — Unregistered commands pass through to subprocess, making cshell2 a drop-in replacement shell for daily use.

6. **Context variables as env vars** — Switching contexts exports variables to `os.environ` and backs up originals. This means subprocesses (system commands) automatically inherit context variables.
