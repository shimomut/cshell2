# Known Limitations & Future Improvements

A living document for cshell2 limitations worth knowing about, and ideas for
future improvements. Add new entries as they come up; once an item is fixed,
either delete it or move it under a "Resolved" subsection with the commit
that addressed it.

## Python commands in pipelines

**Limitation.** A Python `@registry.command` handler cannot participate in a
multi-stage pipeline (any command line containing `|`). The shell only
dispatches through the registry on the single-stage path
(`_execute_stage`); the multi-stage path (`_execute_pipeline`) tokenizes
each stage and hands it directly to `subprocess.Popen`.

**Symptoms.**
- `my_py_cmd | grep foo` → `cshell2: command not found: my_py_cmd` (when no
  external binary of that name exists on `$PATH`).
- `ls | my_py_cmd` → same failure mode for the consumer side.
- Built-ins (`var`, `help`, `cd`, `context`, …) also fail this way inside a
  pipe — they're registered through the same `CommandRegistry`.
- Aliases expand normally but inherit the same wall: an alias whose first
  token resolves to a Python command still misses the registry lookup in a
  pipeline.

**What works today.**
- Single-stage Python commands with redirects (`my_cmd > out.txt`,
  `my_cmd < in.txt 2>&1`). `_execute_stage` swaps
  `sys.stdout`/`sys.stdin`/`sys.stderr` around `cmd.invoke(args)` — see
  [shell.py:1424-1460](../src/cshell2/shell.py#L1424-L1460).

**Related quirks of the redirect path.**
- The redirect branch runs synchronously on the main thread, so
  `passthrough_run` / `passthrough_input` fall back to plain
  `subprocess.run` / `input`. You lose Ctrl+] backgrounding for any
  interactive subprocess spawned from a redirected Python command.
- `SystemExit` raised inside a Python command propagates through the
  redirect path, so e.g. `exit > log` actually exits the shell.

**Improvement idea.** Make `_execute_pipeline` look up
`self.registry.get(tokens[0])` for each stage. For a Python stage:

1. Fork a child (or use `multiprocessing.Process`).
2. In the child, `dup2` the appropriate pipe fds onto fd 0/1, rebind
   `sys.stdin`/`sys.stdout` to match, then call `cmd.invoke(args)`.
3. `os._exit()` with the handler's exit status.

This preserves the existing single-stage redirect semantics and makes
Python commands first-class in pipelines. Open questions:

- How to surface tracebacks from a forked child — capture stderr and
  print on the parent side, or let it go straight to the terminal?
- Windows has no `fork()`. The current Windows path runs Python commands
  synchronously on the main thread; pipelines on Windows would likely
  need a thread-based variant with explicit pipe fds (no shared
  `sys.stdout` mutation across stages).
- `passthrough_run` / `passthrough_input` from inside a piped Python
  command still wouldn't make sense — stdin/stdout are wired to pipes,
  not the terminal. Document that as out-of-scope, or detect and error.
