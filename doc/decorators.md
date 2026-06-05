# Pipeline Decorators

**Status:** design draft — not yet implemented.

A **decorator** is a token of the form `@name` (with optional shell-style flags) at the start of a line that wraps the rest of the line as a pipeline and modifies how that pipeline is run. The leading `@` makes the syntax visually distinct from regular commands, so parsing priority is unambiguous and the construct doesn't collide with POSIX command names.

```
@watch ls | grep py
@watch -n 1 df -h
@time make build
@retry -n 3 flaky-test
@every -i 5s curl https://example.com/health
```

## Prior art: IPython magics

The closest battle-tested analog is **IPython's magic commands**, which have shipped since ~2007 and are now familiar to most Python developers via Jupyter:

| IPython | cshell2 decorator |
|---------|-------------------|
| `%time some_expr` | `@time some_pipeline` |
| `%timeit -n 100 -r 5 expr` | `@timeit -n 100 -r 5 pipeline` |
| `%%capture out` | `@capture out` (or `@quiet`) |
| `%%bash` | (cshell2 already runs shell) |
| `%lsmagic` | `@<TAB>` (decorator-name completion) |

What we borrow:

- **A distinct sigil that can never collide with a command name** (`%` for IPython, `@` for us). This is the core idea — same justification, different character.
- **Shell-style flags after the sigil** (`%timeit -n 100 expr`). IPython users are mostly Python developers and *still* chose flag syntax, because what comes after the magic is code/commands, not a function call. We follow that lead — see "Argument syntax" below.
- **`@<TAB>` discovery** mirrors `%lsmagic` — list everything available without needing docs.
- **User-defined magics via a Python decorator** (`@register_line_magic`) — our `@decorator_registry.decorator(...)` API has the same shape.

What we deliberately don't borrow:

- **Line vs. cell distinction (`%` vs `%%`).** IPython has both because notebook cells are multi-line. cshell2 lines are single statements (modulo `;` and `\`-continuation), so one prefix is enough.
- **Magics receive raw strings.** IPython magics get the rest of the line/cell as a string and parse it themselves, so every magic reinvents arg parsing. We hand decorators a *parsed Pipeline AST* — the shell tokenizes once, decorators stay simple.
- **Auto-magic** (the `%`-less form). IPython lets you write `time expr` if there's no name collision. We skip this — it reintroduces exactly the parsing-priority ambiguity the sigil exists to solve.
- **Implicit return-value capture.** IPython's `%time` interacts with the cell's last expression. There's no expression-vs-statement distinction in a shell, so this doesn't apply.

## Why decorators (vs. a regular built-in command)

The first sketch was to add `watch` as a built-in Python command whose body re-parses its trailing arguments as a cshell2 pipeline. That works for the pipe-with-watch ergonomic problem, but has two real costs:

1. **Syntactic confusion with POSIX `watch`.** Users typing `watch -n 2 ls` reasonably expect the system `watch(1)` semantics — `-d` for diff highlighting, curses-based redraw, no piping by default. A built-in named `watch` either has to mimic POSIX flags faithfully (a maintenance burden and a divergence trap) or silently behave differently from every other shell.
2. **Inconsistent parsing rules.** A built-in `watch` command would have to special-case its argument tokenization: `watch ls | grep py` needs to mean "watch the whole pipeline", but `grep py | watch ls` shouldn't. That makes the rule "where does `|` bind?" depend on which command is on the left, which is the kind of inconsistency that bites users later.

The `@` prefix borrows from Python's decorator syntax: visually obvious, parsed before the normal pipeline grammar runs, no name collision. `watch` (no `@`) still passes through to the system binary unchanged; `@watch` is its own thing.

## Parsing model

Decorators are extracted *before* the pipeline parser runs:

```
raw line
 └─ while the next token starts with '@':
     ├─ peel off '@name' and any decorator-owned flags up to the next non-flag token
     └─ push onto decorator stack
 └─ remainder → existing pipeline parser (unchanged)
```

This means **everything after the decorator(s) is a normal pipeline** — `;`, `&&`, `||`, `|`, `>`, `<`, `2>`, globbing, var expansion, backslash continuation all work exactly as they do at the top level. The decorator never sees raw text; it gets a parsed pipeline AST.

Multiple decorators stack from outside in (closest to the pipeline runs innermost), matching Python's decorator semantics:

```
@time @retry -n 3 flaky-test
       │           └─ retry wraps `flaky-test`
       └─ time wraps `retry -n 3 flaky-test`
```

### Argument syntax

Decorators take **shell-style flags**, not Python kwargs. So `@watch -n 1` rather than `@watch(n=1)`. Reasons:

- IPython's magics chose the same form (`%timeit -n 100 expr`) and it's been ergonomic for ~15 years.
- The token style stays consistent with the rest of the line — flags before the wrapped command, just like flags before any other command's positional args.
- We can reuse `OptionsCompleter` and `arg(...)` directly. No second parser to maintain, no second completion path.

Each decorator declares its flags via the same `arg(...)` helper used for commands. The shell tokenizes the whole line into shell tokens once; everything from `@name` up to the first non-flag token is consumed by the decorator's argparse.

Examples:

```
@watch                    # bare, defaults
@watch -n 1 df -h         # one flag, then pipeline
@watch -n 1 --no-clear ls # multiple flags
@retry -n 3 flaky-test    # positional-style count via -n
@every -i 5s curl …       # interval as a string the decorator parses
```

**Where do the decorator's flags end and the pipeline begin?** First token that doesn't look like a flag (no leading `-`), isn't a value bound to a preceding flag, and isn't another `@name`. This is the same rule argparse already implements; we just stop consuming at the first positional and hand the rest off.

Edge case: a wrapped command whose first token *does* start with `-` (rare — `-` is conventionally an argument, not a command). The decorator can opt out of greedy flag parsing with `params=[arg("--", action="positional_terminator")]`-style sentinels, or the user can write `@watch -- ls -la`. Same convention as POSIX `--`.

## Completion UX

The big win: completion falls through to the existing machinery with almost no new code.

- `@<TAB>` — list registered decorators (with descriptions). Same UI as command-name completion. Mirrors IPython's `%lsmagic`.
- `@watch -<TAB>` — complete the decorator's own flags via the same `OptionsCompleter` machinery commands use.
- `@watch -n <TAB>` — value completion for `-n`, if the decorator's `arg(...)` spec attaches a completer.
- `@watch <TAB>` — first non-flag token after the decorator's args; complete it as a command name. From here on, the existing recipe / argcomplete / cobra fallback chain takes over. `@watch git st<TAB>` works because we're literally asking the git recipe to complete its first arg.
- `@time @ret<TAB>` — second decorator name completes from the registry, same as the first.

No part of this requires the decorator to know about completion. The shell handles `@`-token completion uniformly; once past the decorator(s), it's plain pipeline completion.

## Decorator API sketch

The API reuses the existing `arg(...)` helper from `commands.py` — no parallel `deco_arg`, no parallel parser. A decorator is essentially a command that receives a `Pipeline` instead of running directly.

```python
from cshell2.commands import arg
from cshell2.decorators import registry as decorator_registry

@decorator_registry.decorator(
    name="watch",
    help="Repeatedly run a pipeline until interrupted.",
    params=[
        arg("-n", "--interval", type=float, default=2.0, help="seconds between runs"),
        arg("--no-clear", action="store_true", help="don't clear screen between runs"),
    ],
)
def watch(pipeline, *, interval: float, no_clear: bool):
    while True:
        if not no_clear:
            sys.stdout.write("\x1b[2J\x1b[H")
        pipeline.run()              # runs the parsed pipeline; honours redirects, pipes, etc.
        time.sleep(interval)
```

The `pipeline` argument is the same kind of object the shell's normal execution path runs. It exposes at least:

- `pipeline.run() -> int` — run synchronously, return exit status
- `pipeline.run(stdout=..., stderr=...)` — run with redirected stdio (so `@time` can capture)
- `pipeline.text` — original source text, for display

`Ctrl+C` during a decorator's body interrupts the *current* pipeline iteration; the decorator decides whether to loop again or propagate. (`@watch` would propagate; `@retry` would catch and re-run.)

## Built-in decorators we'd ship

Starter set, ordered by how clearly they motivate the syntax:

| Decorator | Purpose |
|-----------|---------|
| `@watch [-n SEC] [--no-clear]` | re-run pipeline on a timer |
| `@time` | print wall/user/sys time after the pipeline finishes |
| `@retry [-n N]` | re-run on non-zero exit, up to N times |
| `@every -i INTERVAL` | like `@watch` but doesn't clear; logs each run |
| `@quiet [--stderr]` | discard stdout (and optionally stderr) |
| `@bg` | run pipeline in a fresh background context (replaces `&`) |
| `@as NAME` | run pipeline in a named context (creating it if needed) |

`@bg` and `@as` are interesting because they tie into cshell2's existing context-multiplexing primitives — a decorator becomes the natural surface for "run this pipeline in a different process slot."

## UX questions to nail down

1. ~~**Args syntax** — Python kwargs vs. shell-style?~~ **Resolved:** shell-style flags via the existing `arg(...)` helper. Matches IPython precedent and reuses `OptionsCompleter` directly.
2. **Stacking direction** — Python convention is outer-first (top decorator wraps last). Match that, or invert because shell users read left-to-right "what happens first"?
3. **Where does decorator output go relative to pipeline output?** If `@time` prints timing info, does it go to stderr by default? After the pipeline? Interleaved?
4. **Interaction with `;` and `&&`.** Does `@watch ls; pwd` watch `ls; pwd` together, or just `ls` and then run `pwd` once? Probably the former (decorator wraps the whole *statement*), but worth being explicit. A decorator could also opt in to seeing only the first pipeline.
5. **Redirects and the decorator.** `@time make > build.log 2>&1` — does `@time`'s output go into `build.log` too? Probably no: redirects bind to the pipeline, not the decorator. Decorator output goes to the original stdio.
6. **Ctrl+] context switching while a decorator is looping.** `@watch` running `ls` should be backgroundable like any other long-running command. Does the decorator body run on a `PythonCommandSlot`? (Probably yes — it's a Python command in everything but syntax.)
7. **Where do decorator flags end and the pipeline begin?** First non-flag token; `--` works as an explicit terminator (same as POSIX). Mostly resolved by reusing argparse, but worth a parser test for edge cases like `@watch ls -la` (does `-la` belong to `watch` or `ls`? — answer: `ls`, since `watch` declares no `-l`).
8. **History.** Does `@watch ls` get stored in history as written, or does each iteration land in history? (Almost certainly: stored once as written.)
9. **Reload semantics.** Does `reload` re-register user decorators alongside commands and vars? Same lifecycle as `clear_user_commands()`.
10. **Error messages.** `@watche ls` — typo. Do we suggest `@watch`? Plain command typos already get a "command not found" via the system shell; for decorators we own the lookup, so we can be friendlier.

## Non-goals (for the first cut)

- **Not a general macro system.** Decorators wrap a parsed pipeline; they don't rewrite source text or define new syntax.
- **Not a replacement for shell functions or aliases.** Aliases are name-for-text substitution; decorators are runtime wrappers around a pipeline AST. They're complementary.
- **Not POSIX-compatible.** The `@` prefix is intentionally cshell2-specific; scripts using decorators won't run in `bash`/`zsh`. That's fine — cshell2 is interactive-first.

## Implementation sketch (to flesh out after design lock)

1. **Tokenizer change** in `pipeline.py`: at line start, while the next token is `@<ident>`, peel off the decorator name plus any following flag tokens its argparse spec consumes; push onto a decorator stack. Continue parsing the rest as a normal line.
2. **`decorators.py` module** mirroring `commands.py` / `variables.py`: `Decorator` ABC, `DecoratorRegistry`, module-level `registry` singleton, `@registry.decorator(...)` API. Reuses `arg(...)` from `commands.py` directly — no new helper.
3. **Pipeline AST handle**: factor out the existing pipeline-execution path in `shell.py` into a `Pipeline` object that decorators can call `.run()` on. Today the parsed structure is consumed inline; this would give it a proper boundary.
4. **Completion glue** in `completion.py`: detect the `@` prefix in `CompletionContext` and dispatch to a `DecoratorNameCompleter` for the name, then to the decorator's `OptionsCompleter` for its flags. Once past the decorator's flags, strip the decorator portion from `ctx.line`/`ctx.args` and delegate to the normal completion pipeline.
5. **Built-in decorators** in `cshell2/decorators/` (parallel to `recipes/`): `watch.py`, `time.py`, `retry.py`, etc. `mark_builtins()` analog to keep them across `reload`.
6. **Tests**: `tests/test_decorators.py` for parsing edge cases (stacked, with flags, with pipes, with redirects, with continuation lines, with `--` terminator), plus completion tests for the `@`-token codepath.
