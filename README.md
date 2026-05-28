# cshell2

A lightweight but powerful terminal shell environment with rich tab completion and context switching.

## Features

- **Rich tab completion** — per-argument completers with descriptions, inline picker UI
- **Multi-select flag picker** — TAB on flags opens a Space-to-toggle checkbox list
- **Context switching** — named environments with variables and working directories, with push/pop and `Ctrl+]` live switching
- **PTY process multiplexing** — run processes in contexts and switch between them without killing them
- **Custom commands** — define Python functions as shell commands with full completion support
- **Completion recipes** — opt-in TAB completion for `git`, `make`, `ssh`, `aws`, and more
- **Protocol fallbacks** — automatic completion for cobra-based tools (`docker`, `kubectl`, `helm`, `gh`, …) and argcomplete-based Python CLIs (`pipx`, `conda`, `pre-commit`, `tox`, …) — no recipe needed
- **System command fallback** — anything not a registered command runs through the system shell
- **History** — persistent history with up/down navigation and `Ctrl+R` search

## Installation

Requires Python 3.12+.

```bash
pip install -e .
```

## Usage

```bash
cshell2
```

### Built-in Commands

| Command | Description |
|---------|-------------|
| `cd [path]` | Change directory (default: home) |
| `help [command]` | Show help for a command or list all commands |
| `context` | Manage contexts (see below) |
| `var [KEY=VALUE ...]` | Set context variables, or list all env vars |
| `unset KEY [KEY ...]` | Unset context variables |
| `reload` | Reload `~/.cshell2/config.py` without restarting |
| `exit` | Exit the shell |

Any command not listed above is passed through to the system shell (e.g., `ls`, `git`, `grep`).

### Contexts

Contexts let you define named environments with variables that are exported to `os.environ` and a remembered working directory.

```
cshell2> context push prod
Pushed context 'prod'
[prod] cshell2> var ACCOUNT=123456 REGION=us-east-1
[prod] cshell2> context push staging
Pushed context 'staging'
[staging] cshell2> var ACCOUNT=789012 REGION=us-west-2
[staging] cshell2> context pop
Popped 'staging', now in 'prod'
[prod] cshell2> context list
  * prod {'ACCOUNT': '123456', 'REGION': 'us-east-1'}
    staging {'ACCOUNT': '789012', 'REGION': 'us-west-2'}
```

Context subcommands:

| Subcommand | Description |
|------------|-------------|
| `context push <name>` | Create a new context (inheriting current vars) and switch to it |
| `context pop` | Return to the previous context and remove the current one |
| `context switch <name>` | Switch to an existing context without modifying the stack |
| `context list` | Show all contexts with their state and variables |
| `context kill <name>` | Send SIGTERM to the running process in a context |

### Context Switching with Ctrl+]

Press `Ctrl+]` at the shell prompt (or while a process is running) to open a TUI context picker:

- Arrow keys or `Ctrl+P/N` to navigate
- **Enter** to switch to the selected context
- **Esc** / `Ctrl+C` to cancel
- Select `+ new context` to create a new one

If the target context has a running process, switching to it resumes that process immediately. The original process keeps running in the background — visible as `[bg:1]` in the prompt.

### Tab Completion

Press TAB to complete:
- Command names (registered commands + system PATH executables)
- File/directory paths (default fallback)
- Custom per-argument completions defined by commands

**Flag completion** — when flags are available, TAB opens a multi-select checkbox picker:
- Navigate with arrows; **Space** toggles a flag; **Enter** confirms
- Type a letter to jump to the next flag starting with that letter
- Short boolean flags are combined: selecting `-a` and `-l` inserts `-al`
- Flags that take a value (e.g. `-d N`) insert `flag ` then open a value picker or show an inline hint

## Customization

Create `~/.cshell2/config.py` to define custom commands and completers. This file is plain Python that imports from cshell2. Use `reload` to apply changes without restarting.

```python
# ~/.cshell2/config.py
from cshell2.commands import registry, arg
from cshell2.completion import Completer, Completion, ChoiceCompleter
from cshell2.recipes import enable

# Enable TAB completion for system commands
enable("make", "git", "ssh")

class InstanceCompleter(Completer):
    def complete(self, ctx):
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("ACCOUNT")
        # fetch instances for account...
        return [Completion(value="i-abc123", description="web-server-1")]

@registry.command(
    name="connect",
    help="SSH into an EC2 instance.",
    params=[
        arg("account", choices=["prod", "staging"]),
        arg("region",  choices=["us-east-1", "us-west-2"]),
        arg("instance_id", completer=InstanceCompleter()),
    ],
)
def connect(account, region, instance_id):
    import os
    os.system(f"ssh {instance_id}")
```

### Prompt Customization

The prompt is generated by a Python function you can override with `set_prompt()`. The default prompt shows the context name (if not `"default"`), current directory (up to 2 levels), a timestamp, and `[bg:N]` when N other contexts have running processes:

```
[prod] projects/cshell2 14:32:07>
```

To customize, define a function that takes a `ContextManager` and returns a string:

```python
# ~/.cshell2/config.py
import os
from datetime import datetime
from cshell2 import set_prompt

def my_prompt(context_manager):
    ctx = context_manager.current()
    prefix = f"({ctx.name}) " if ctx else ""
    cwd = os.path.basename(os.getcwd()) or "/"
    time = datetime.now().strftime("%H:%M")
    return f"{prefix}{cwd} [{time}]$ "

set_prompt(my_prompt)
```

The function is called each time the prompt is displayed, so it reflects dynamic state like the current directory, time, or context variables.

### Available Completers

| Completer | Description |
|-----------|-------------|
| `ChoiceCompleter(items)` | Complete from a static list |
| `CallbackCompleter(func)` | Complete from a function's return value |
| `FileCompleter()` | Complete filesystem paths (files and directories) |
| `DirCompleter()` | Complete directory paths only |
| `OptionsCompleter(options, args)` | Complete flags with multi-select TUI; `args` declares value-taking flags |
| `ConditionalCompleter(mapping)` | Pick a sub-completer based on preceding args |

### Completion Recipes

Built-in recipes add TAB completion for common system commands. Enable them in `~/.cshell2/config.py`:

```python
from cshell2.recipes import enable
enable("git", "make", "ssh", "kill", "ls", "grep", "find", "du", "df", "tail", "aws")
```

Each recipe registers flag completion (via `OptionsCompleter`) and positional completions (subcommands, files, branches, etc.) for the named command.

Two protocol fallbacks activate automatically — no recipe needed:

- **Cobra-based tools** (`docker`, `kubectl`, `helm`, `gh`, `argocd`, …) — `CobraCompleter` drives their `__complete` subcommand, including live resource enumeration (running containers, k8s resources, GitHub issues, …). See [doc/cobra-fallback.md](doc/cobra-fallback.md).
- **argcomplete-based Python CLIs** (`pipx`, `conda`, `pre-commit`, `tox`, `pdm`, `httpie`, …) — `ArgcompleteCompleter` detects the `# PYTHON_ARGCOMPLETE_OK` marker and drives the argcomplete protocol. See [doc/argcomplete-fallback.md](doc/argcomplete-fallback.md).

#### User-Defined Recipes

You can write your own recipes and place them in `~/.cshell2/recipes/` (or any directory you add to the search path). `enable()` checks the search path automatically after the built-ins, so the call site in `config.py` is identical:

```python
from cshell2.recipes import enable
enable("git")          # built-in
enable("my_tool")      # found in ~/.cshell2/recipes/my_tool.py
```

A recipe file must define a `register()` function:

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
    # Return dynamic values (cached, fetched from an API, etc.)
    return ["web", "worker", "scheduler"]
```

#### Recipe Search Path

The default search path contains only `~/.cshell2/recipes/`. Call `add_recipe_path()` to add more directories — useful for sharing recipes across a team:

```python
from cshell2.recipes import add_recipe_path, enable

add_recipe_path("/team/shared/recipes")   # checked after ~/.cshell2/recipes/
enable("my_tool")   # found in whichever directory contains my_tool.py first
```

Lookup order for every `enable()` call:

1. Built-in package (`cshell2.recipes.<name>`) — always highest priority
2. `~/.cshell2/recipes/<name>.py` — personal recipes
3. Additional paths in the order they were added via `add_recipe_path()`

You can also read or modify `recipe_search_path` directly (it is a plain `list[Path]`).

### Writing a Custom Completer

Subclass `Completer` and implement `complete()`. The `CompletionContext` gives you:

- `command` — the command being completed for
- `args` — previously completed arguments
- `arg_index` — which argument position is being completed
- `prefix` — partial text typed so far
- `shell_context` — the active context (access variables with `.get_variable()`)

To add completion to a system command without wrapping it:

```python
from cshell2.commands import registry
from cshell2.completion import OptionsCompleter, FileCompleter

registry.register_external_completers("mytools", {
    None: OptionsCompleter({"-v": "verbose", "--output": "output file"},
                           args={"--output": "FILE"}),
    0: FileCompleter(),
})
```

### Key Bindings

| Key | Action |
|-----|--------|
| `Tab` | Open completion picker |
| `Ctrl+R` | Search history |
| `Ctrl+]` | Open context switcher |
| `↑` / `↓` or `Ctrl+P/N` | Navigate history |
| `Ctrl+A` / `Ctrl+E` | Move to start / end of line |
| `Ctrl+B` / `Ctrl+F` | Move one character left / right |
| `Alt+B` / `Alt+F` | Move one word left / right |
| `Ctrl+W` | Delete word before cursor |
| `Ctrl+K` | Delete to end of line |
| `Ctrl+U` | Delete to beginning of line |
| `Ctrl+L` | Clear screen |
| `Ctrl+D` | Exit (on empty line) |

## File Locations

| Path | Purpose |
|------|---------|
| `~/.cshell2/config.py` | User configuration |
| `~/.cshell2/history` | Command history |
| `~/.cshell2/recipes/<name>.py` | User-defined completion recipes (loaded by `enable("<name>")`) |
