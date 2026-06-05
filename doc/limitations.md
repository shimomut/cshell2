# Known Limitations & Future Improvements

A living document for cshell2 limitations worth knowing about, and ideas for
future improvements. Add new entries as they come up; once an item is fixed,
either delete it or move it under a "Resolved" subsection with the commit
that addressed it.

## Python commands in pipelines — caveats of the in-process model

Python `@registry.command` handlers participate in pipelines as worker
threads sharing the shell process — `_execute_pipeline` looks up each
stage in the registry, runs registered commands in a thread that rebinds
`sys.stdin` / `sys.stdout` / `sys.stderr` to the pipe ends via the
thread-local routers in `shell.py`, and waits on a mixed list of
`subprocess.Popen` and Python-stage handles. No `fork()`; works the
same on POSIX and Windows.

The remaining caveats below are inherent to the "stay in-process" choice;
fixing them would require a separate process per Python stage.

**Nested `subprocess` writes to the terminal, not the pipe.**

```python
@registry.command(name="my_cmd")
def my_cmd():
    print("hello")              # → goes through the pipe ✓
    subprocess.run(["echo", "x"])  # → writes to the terminal ✗
```

`subprocess` reads the *real* fd 1, not the Python `sys.stdout` object
the thread-local router rebinds. Workaround: pass `stdout=sys.stdout`
(and `stdin=sys.stdin`, `stderr=sys.stderr` as needed) explicitly when
shelling out from a piped Python command. The same caveat applies to
the single-stage redirect path — `my_cmd > out.txt` redirects `print`
but not nested `subprocess` output.

**Stateful built-ins mutate the parent in pipelines.**

`cd | tee log` actually changes the shell's CWD; `var X=1 | …` actually
sets the variable; `context push | …` actually pushes a context. POSIX
shells run each stage in a subshell, so these mutations are normally
discarded — cshell2 does not. Treat this as the cost of the in-process
model: the change is visible.

**Pure-CPU loops in a Python command can't be Ctrl+C'd in a pipeline.**

The pipeline driver catches `KeyboardInterrupt` and closes pipe ends to
unblock I/O-bound stages, which is enough for the common case (the
worker's next read/write raises `BrokenPipeError`/`OSError` and the
thread unwinds). A stage that's running a tight Python loop with no
I/O won't notice — Python doesn't support cancelling a thread. If a
command wants to be interruptible without I/O, it needs to check for
some flag or use `signal.set_wakeup_fd`-style coordination itself.

**`passthrough_run` / `passthrough_input` are not usable in piped Python
commands** — stdin/stdout are wired to pipes, not the terminal, so
those helpers can't do their job. They raise `RuntimeError` if called
from inside a pipeline thread. Use plain `subprocess.run` (with the
`stdout=sys.stdout` workaround above) for non-interactive children.

**`SystemExit` raised in a redirected single-stage Python command still
exits the shell.** `exit > log` exits the shell because the redirect
path on `_execute_stage` runs synchronously on the main thread. The
pipeline path catches and absorbs `SystemExit` per stage; matching
that behaviour for the redirect path is a separate, smaller change.
