# Pipeline Decorators

**Status:** design draft — not yet implemented.

A **decorator** is a token of the form `@name` (or `@name(args)`) at the start of a line that wraps the rest of the line as a pipeline and modifies how that pipeline is run. The leading `@` makes the syntax visually distinct from regular commands, so parsing priority is unambiguous and the construct doesn't collide with POSIX command names.

```
@watch ls | grep py
@watch(n=1) df -h
@time make build
@retry(3) flaky-test
@every(5s) curl https://example.com/health
```

## Why decorators (vs. a regular built-in command)

The first sketch was to add `watch` as a built-in Python command whose body re-parses its trailing arguments as a cshell2 pipeline. That works for the pipe-with-watch ergonomic problem, but has two real costs:

1. **Syntactic confusion with POSIX `watch`.** Users typing `watch -n 2 ls` reasonably expect the system `watch(1)` semantics — `-d` for diff highlighting, curses-based redraw, no piping by default. A built-in named `watch` either has to mimic POSIX flags faithfully (a maintenance burden and a divergence trap) or silently behave differently from every other shell.
2. **Inconsistent parsing rules.** A built-in `watch` command would have to special-case its argument tokenization: `watch ls | grep py` needs to mean "watch the whole pipeline", but `grep py | watch ls` shouldn't. That makes the rule "where does `|` bind?" depend on which command is on the left, which is the kind of inconsistency that bites users later.

The `@` prefix borrows from Python's decorator syntax: visually obvious, parsed before the normal pipeline grammar runs, no name collision. `watch` (no `@`) still passes through to the system binary unchanged; `@watch` is its own thing.

## Parsing model

Decorators are extracted *before* the pipeline parser runs:

```
raw line
 └─ if first token starts with '@':
     ├─ peel off @name(args)? prefix
     └─ remainder → existing pipeline parser (unchanged)
 └─ else:
     └─ existing pipeline parser (unchanged)
```

This means **everything after the decorator is a normal pipeline** — `;`, `&&`, `||`, `|`, `>`, `<`, `2>`, globbing, var expansion, backslash continuation all work exactly as they do at the top level. The decorator never sees raw text; it gets a parsed pipeline AST.

Multiple decorators stack from outside in (closest to the pipeline runs innermost), again matching Python:

```
@time @retry(3) flaky-test
       │       └─ retry wraps `flaky-test`
       └─ time wraps `retry(3) flaky-test`
```

### `@name` vs `@name(args)`

- `@watch` — bare form, decorator uses defaults
- `@watch(n=1)` — keyword-style args, parsed as a comma-separated kwarg list
- `@retry(3)` — positional args allowed
- `@every(5s)` — value with a unit suffix; the decorator owns interpretation

Argument tokenization inside the parens is **decorator-local**. The shell doesn't expand `$VAR` or globs inside `()`; whatever's between the matching parens is handed to the decorator as a string for it to parse however it likes (probably via a small declarative spec, see below).

Open question: do we want shell-style args (`@retry -n 3`) instead of / in addition to Python-style? Python-style reads more like the decorator analogue we're borrowing from, but shell-style is more familiar to terminal users and needs no extra parser. Leaning Python-style for now since the whole point of the syntax is to look unlike a command.

## Completion UX

The big win: completion falls through to the existing machinery with almost no new code.

- `@<TAB>` — list registered decorators (with descriptions). Same UI as command-name completion.
- `@watch <TAB>` — first non-decorator token; complete it as a command name. From here on, the existing recipe / argcomplete / cobra fallback chain takes over. `@watch git st<TAB>` works because we're literally asking the git recipe to complete its first arg.
- `@watch(<TAB>` — complete decorator's own kwargs (`n=`, …). The decorator declares them; completion reads from that spec.
- `@watch(n=<TAB>` — value completion for the kwarg, if the decorator's spec provides a completer for it.
- `@time @ret<TAB>` — second decorator name completes from the registry, same as the first.

No part of this requires the decorator to know about completion. The shell handles `@`-token completion uniformly; once past the decorator(s), it's plain pipeline completion.

## Decorator API sketch

```python
from cshell2.decorators import registry as decorator_registry, deco_arg

@decorator_registry.decorator(
    name="watch",
    help="Repeatedly run a pipeline until interrupted.",
    params=[
        deco_arg("n", type=float, default=2.0, help="seconds between runs"),
        deco_arg("clear", type=bool, default=True, help="clear screen between runs"),
    ],
)
def watch(pipeline, *, n: float, clear: bool):
    while True:
        if clear:
            sys.stdout.write("\x1b[2J\x1b[H")
        pipeline.run()              # runs the parsed pipeline; honours redirects, pipes, etc.
        time.sleep(n)
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
| `@watch(n=2.0, clear=True)` | re-run pipeline on a timer |
| `@time` | print wall/user/sys time after the pipeline finishes |
| `@retry(n=3, on=...)` | re-run on non-zero exit, up to N times |
| `@every(interval)` | like `@watch` but doesn't clear; logs each run |
| `@quiet` | discard stdout (and optionally stderr) |
| `@bg` | run pipeline in a fresh background context (replaces `&`) |
| `@as(name)` | run pipeline in a named context (creating it if needed) |

`@bg` and `@as` are interesting because they tie into cshell2's existing context-multiplexing primitives — a decorator becomes the natural surface for "run this pipeline in a different process slot."

## UX questions to nail down

1. **Args syntax** — Python kwargs (`@watch(n=1)`) vs. shell-style (`@watch -n 1`)? Mixed?
2. **Stacking direction** — Python convention is outer-first (top decorator wraps last). Match that, or invert because shell users read left-to-right "what happens first"?
3. **Where does decorator output go relative to pipeline output?** If `@time` prints timing info, does it go to stderr by default? After the pipeline? Interleaved?
4. **Interaction with `;` and `&&`.** Does `@watch ls; pwd` watch `ls; pwd` together, or just `ls` and then run `pwd` once? Probably the former (decorator wraps the whole *statement*), but worth being explicit. A decorator could also opt in to seeing only the first pipeline.
5. **Redirects and the decorator.** `@time make > build.log 2>&1` — does `@time`'s output go into `build.log` too? Probably no: redirects bind to the pipeline, not the decorator. Decorator output goes to the original stdio.
6. **Ctrl+] context switching while a decorator is looping.** `@watch` running `ls` should be backgroundable like any other long-running command. Does the decorator body run on a `PythonCommandSlot`? (Probably yes — it's a Python command in everything but syntax.)
7. **Completion of the decorator's *own* kwargs vs. the wrapped command.** When the cursor is inside `@watch(`, complete kwargs; once past `)`, complete the wrapped command. The split has to be unambiguous to the tokenizer — paren-balance rules apply.
8. **History.** Does `@watch ls` get stored in history as written, or does each iteration land in history? (Almost certainly: stored once as written.)
9. **Reload semantics.** Does `reload` re-register user decorators alongside commands and vars? Same lifecycle as `clear_user_commands()`.
10. **Error messages.** `@watche ls` — typo. Do we suggest `@watch`? Plain command typos already get a "command not found" via the system shell; for decorators we own the lookup, so we can be friendlier.

## Non-goals (for the first cut)

- **Not a general macro system.** Decorators wrap a parsed pipeline; they don't rewrite source text or define new syntax.
- **Not a replacement for shell functions or aliases.** Aliases are name-for-text substitution; decorators are runtime wrappers around a pipeline AST. They're complementary.
- **Not POSIX-compatible.** The `@` prefix is intentionally cshell2-specific; scripts using decorators won't run in `bash`/`zsh`. That's fine — cshell2 is interactive-first.

## Implementation sketch (to flesh out after design lock)

1. **Tokenizer change** in `pipeline.py`: at line start, if first token matches `@<ident>(\(...\))?`, peel it off into a decorator list and continue parsing the rest as a normal line. Repeat for stacked decorators.
2. **`decorators.py` module** mirroring `commands.py` / `variables.py`: `Decorator` ABC, `DecoratorRegistry`, module-level `registry` singleton, `@registry.decorator(...)` API, `deco_arg(...)` helper for kwargs.
3. **Pipeline AST handle**: factor out the existing pipeline-execution path in `shell.py` into a `Pipeline` object that decorators can call `.run()` on. Today the parsed structure is consumed inline; this would give it a proper boundary.
4. **Completion glue** in `completion.py`: detect the `@` prefix in `CompletionContext` and dispatch to a `DecoratorNameCompleter` / kwarg completer, then delegate the rest of the line to the normal completion pipeline with the decorator portion stripped from `ctx.line`/`ctx.args`.
5. **Built-in decorators** in `cshell2/decorators/` (parallel to `recipes/`): `watch.py`, `time.py`, `retry.py`, etc. `mark_builtins()` analog to keep them across `reload`.
6. **Tests**: `tests/test_decorators.py` for parsing edge cases (stacked, with kwargs, with pipes, with redirects, with continuation lines), plus completion tests for the `@`-token codepath.
