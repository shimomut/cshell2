# Plan: bash-completion Fallback for Unrecognized Commands

## Status

Planning only — not yet implemented.

## Motivation

cshell2 currently provides TAB completion through two paths:

1. Python commands registered with `@registry.command(params=[arg(...)])`
2. External completer recipes in `src/cshell2/recipes/` (git, docker, ssh, …)

Anything outside those two paths falls through to `FileCompleter` only. That covers a lot of daily usage but leaves a long tail of system tools (kubectl, npm, pip, cargo, terraform, helm, gh, …) without smart completions unless someone writes a recipe.

Most of these tools ship a **bash-completion** script that already encodes their completion logic. If we can drive those scripts from a subprocess and surface the results through cshell2's `Completer` interface, we get useful completions for hundreds of commands "for free" — a high-value fallback layer that activates only when no recipe is registered.

zsh's own completion system (`compdef`/`_git`) is much harder to reuse from outside an interactive zsh — its functions write into zsh-internal state (`_describe`, `compadd`) instead of returning a list. Embedding zsh would be heavyweight. **bash-completion** is the right target: it's a documented protocol with stable environment-variable inputs and a single output array.

## Goals

- Provide useful TAB completions for any command that has a bash-completion script installed, without writing a per-command recipe.
- Activate **only when no native recipe is registered** — recipes always win.
- Stay within the existing `Completer` protocol; no changes to `lineedit.py` or `tui.py`.
- Bound the cost: a TAB press should never block the prompt for more than a small, configurable timeout.
- Fail silently when bash, the bash-completion package, or the per-tool script is missing — fall back to `FileCompleter`.

## Non-goals

- Reusing zsh `_git`-style completion functions. They are not designed to be driven externally.
- Matching every nuance of bash's completion behavior (e.g. `compopt -o nospace` per-completion suffix control, `_filedir` quoting subtleties). We'll get the common case right.
- Surfacing per-completion descriptions. The bash-completion protocol returns plain strings — no descriptions, no flag/value distinction. Recipes will continue to be the way to get rich UX (descriptions, multi-select pickers, arg-hint prompts).
- Caching across shell restarts. In-memory caching only.

## How bash-completion works

A bash-completion script registers a completion function for a command via:

```bash
complete -F _git git
```

When the user presses TAB, bash sets these variables and calls `_git`:

| Variable | Meaning |
|----------|---------|
| `COMP_LINE`  | the entire command line |
| `COMP_POINT` | byte offset of the cursor within `COMP_LINE` |
| `COMP_WORDS` | the line tokenized into an array |
| `COMP_CWORD` | index in `COMP_WORDS` of the word being completed |

The function appends candidates to the `COMPREPLY` bash array. Bash then renders them.

To drive this from outside an interactive bash, we run a non-interactive `bash -c` with the right setup:

```bash
source /usr/share/bash-completion/bash_completion
# Trigger lazy loading for $cmd (bash-completion v2 ships per-command files
# under completions/ that are autoloaded on first use).
_completion_loader git 2>/dev/null
COMP_LINE='git che'
COMP_POINT=${#COMP_LINE}
COMP_WORDS=(git che)
COMP_CWORD=1
# Look up the function bash-completion registered for 'git'.
_func=$(complete -p git 2>/dev/null | sed -n 's/.*-F \([^ ]*\).*/\1/p')
[[ -n $_func ]] && "$_func"
printf '%s\n' "${COMPREPLY[@]}"
```

The output is one candidate per line on stdout; everything else (errors, sourcing noise) goes to stderr and is discarded.

### Where bash-completion lives

| Platform | Common path |
|----------|-------------|
| macOS (Homebrew, bash 5)  | `/opt/homebrew/etc/profile.d/bash_completion.sh` (Apple Silicon) or `/usr/local/etc/profile.d/bash_completion.sh` (Intel) |
| macOS (Homebrew, v1, bash 3.2) | `/usr/local/etc/bash_completion` |
| Linux (Debian/Ubuntu)     | `/usr/share/bash-completion/bash_completion` |
| Linux (RHEL/Fedora)       | `/usr/share/bash-completion/bash_completion` |
| Nix                       | `${pkgs.bash-completion}/share/bash-completion/bash_completion` |

We probe a list of paths at startup (or first use) and pick the first one that exists. Users can override via the `CSHELL2_BASH_COMPLETION` env var.

Per-command scripts (under `…/bash-completion/completions/<cmd>`) are autoloaded by `_completion_loader` in v2; in v1 we have to source them manually.

## Design sketch

### One new completer

```python
# src/cshell2/completion.py  (new class — same module as the other built-ins)

class BashCompletionCompleter(Completer):
    """Fallback completer that drives system bash-completion scripts.

    Activated by the shell when:
      * a command name was typed (ctx.command is set),
      * no completer is registered for ctx.command in the registry,
      * bash and the bash-completion package are available on the host.

    Returns plain Completion values with no description and no multi_select
    (so the engine renders an InlinePicker exactly as today).
    """

    def __init__(self, *, timeout: float = 1.5,
                 bash_path: str | None = None,
                 init_script: str | None = None) -> None:
        self._timeout = timeout
        self._bash_path = bash_path or _detect_bash()
        self._init_script = init_script or _detect_init_script()
        self._cache: dict[tuple[str, str, int], list[str]] = {}

    def should_activate(self, ctx: CompletionContext) -> bool:
        return bool(self._bash_path and self._init_script and ctx.command)

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        key = (ctx.line, ctx.prefix, len(ctx.args))
        if key in self._cache:
            words = self._cache[key]
        else:
            words = self._invoke_bash(ctx)
            self._cache[key] = words
        return [Completion(value=w) for w in words if w.startswith(ctx.prefix)]
```

`_invoke_bash` builds a small bash script (the snippet shown earlier) and runs it with `subprocess.run(..., timeout=self._timeout, capture_output=True, text=True)`. On `TimeoutExpired`, `OSError`, or non-zero exit, return `[]`.

### Wiring into the dispatch order

In `shell.py`'s `_get_completions` (or wherever the registered-completer lookup happens), after the existing fallback chain fails to find a registered completer for the current `arg_index` and the options path doesn't apply, **try `BashCompletionCompleter` before `FileCompleter`**. If `BashCompletionCompleter` returns a non-empty list, use it; otherwise keep the existing `FileCompleter` fallback.

Concretely, the chain becomes:

```
1. Recipe / @registry.command completer for this position?  → use it
2. None-key OptionsCompleter and prefix starts with "-"?     → use it
3. Bash-completion fallback enabled and ctx.command is set?  → try it
   - non-empty result            → use it
   - empty result or unavailable → fall through
4. FileCompleter fallback                                    → use it
```

The completer is instantiated once at shell startup and stored on the shell object so its in-memory cache persists across TAB presses.

### Disabling

A new entry under `[completion]` (or as a top-level switch — TBD when we touch config) controls the fallback:

```python
# ~/.cshell2/config.py
from cshell2.completion import disable_bash_completion_fallback
disable_bash_completion_fallback()
```

Default: **enabled** if bash + bash-completion are detected, **disabled** otherwise. The detection happens once at shell startup; failure is silent.

### What we send to bash

For a line like `git che` with cursor at the end of `che`:

| Variable | Value |
|----------|-------|
| `COMP_LINE`  | `git che` |
| `COMP_POINT` | `7` |
| `COMP_WORDS` | `(git che)` |
| `COMP_CWORD` | `1` |

For `git commit -m "hello wor` (cursor at end), we have to be careful — `COMP_WORDS` should reflect bash's tokenization, which respects quotes. Using cshell2's existing `split_for_completion()` would diverge from bash's parser and produce wrong completions inside quotes. **Decision:** delegate tokenization to bash itself by passing `COMP_LINE` and `COMP_POINT`, then letting the bash runner compute `COMP_WORDS`/`COMP_CWORD` with `_get_comp_words_by_ref` (a helper bash-completion provides). This keeps quoting/escaping behavior consistent with what users expect.

### Example output

For `kubectl get po<TAB>` on a host with `kubectl` bash-completion installed:

```
pod
pods
poddisruptionbudget
poddisruptionbudgets
```

cshell2 wraps each in `Completion(value=…)` and presents an `InlinePicker` exactly as today. No description column, no multi-select — that's fine for a fallback.

## Cost and tradeoffs

| Aspect | Assessment |
|--------|------------|
| Latency | One `bash -c` invocation per TAB press. On a warm system, the bash startup + sourcing bash-completion is roughly **30–80 ms**. The per-command function (e.g. `_git`, `_kubectl`) can add **20–500 ms** depending on what it does (some shell out to the tool itself). Cap with `timeout`. |
| Caching | Keyed on `(line, prefix, arg_count)`. A single TAB session that narrows the list reuses the cached candidate set — only the prefix filter changes. Cache cleared at shell exit. |
| Correctness | Tokenization handled by bash, so quoting/escaping match user expectation. `compopt -o nospace` and similar per-completion modifiers are not honored — we always treat the result as a plain word. |
| UX gap vs. recipes | No descriptions, no multi-select flag picker, no arg-hint prompt. Recipes remain the right path for tools we use heavily. |
| Failure modes | If bash isn't installed, init script missing, or the per-tool script throws → return empty, fall through to `FileCompleter`. Never crashes the prompt. |
| Security | We `source` user-installed bash-completion scripts. These are already trusted by the user's bash; no new attack surface. We never `eval` user input — `COMP_LINE` is set as an environment variable, not interpolated into shell. |

## Open questions

1. **Init-script detection across distros.** A reasonable fixed probe list should cover macOS Homebrew + common Linuxes. Should we also honor `BASH_COMPLETION_USER_FILE` if set?
2. **Per-command timeout vs. global timeout.** Some completion functions (e.g. older AWS CLI) are slow. Default to 1.5s; allow override via the config call site.
3. **Cache invalidation.** Some completions reflect live state (running containers, current branch). Today the user works around this by pressing TAB once, getting fresh data, and the cache only helps within one session of typing. That's probably fine — but document it.
4. **Should the fallback also fire for commands that *do* have a recipe but the recipe returned empty?** Probably no — recipes are a deliberate "no completions here" signal in some cases (per the engine's existing rule that empty-from-registered-completer suppresses fallback). Keeping that rule intact means: bash fallback only fires when no recipe exists at all.
5. **Windows / fish / pwsh.** Out of scope. The detection routine returns `None` and the fallback is silently disabled.

## Implementation roadmap

When we're ready to implement:

1. **`_detect_bash` / `_detect_init_script` helpers** in `completion.py` — module-level functions, run once.
2. **`BashCompletionCompleter` class** — implements the protocol, holds the cache.
3. **The bash runner script** — embedded as a constant string in `completion.py`. Keep it short and POSIX-bash-compatible (no bashisms beyond what bash-completion itself requires).
4. **Wire into the dispatch chain** in `shell.py`'s `_get_completions` — between registered-completer lookup and `FileCompleter` fallback.
5. **Config switch** — `disable_bash_completion_fallback()` (and a corresponding enable) exported from `cshell2.completion`.
6. **Tests** — `tests/test_bash_completion.py`:
   - Mock `subprocess.run` to return a known stdout, assert candidates parsed correctly.
   - Cover the timeout path, the missing-init-script path, the empty-result path.
   - Skip live integration tests if bash-completion isn't on the test runner.
7. **Doc updates** — add a one-paragraph note in [completion.md](completion.md) under "How TAB Completion Works", and a callout in [recipes.md](recipes.md) explaining when to write a recipe vs. rely on the fallback.

## Why this is a fallback, not a replacement

Recipes will continue to deliver the best UX — descriptions in the picker, multi-select for flag clusters, value pickers driven by `arg_hint`, context-aware completers that read `ctx.shell_context`. Bash-completion can't express any of that.

The fallback's job is to make the **long tail** of unfamiliar commands feel useful out of the box, so users don't hit a wall of plain `FileCompleter` for tools they haven't written a recipe for yet.
