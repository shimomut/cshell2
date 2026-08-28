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

## `awsut sagemaker jobs` — a category the loaded model doesn't declare costs a round-trip

`JobCategory` is required by ListJobs and DescribeJob, so a job cannot be
looked up by name alone: with no `--category`, the commands query every
category they know about. That set is the loaded botocore model's
`ListJobs` enum plus anything in `jobs.EXTRA_JOB_CATEGORIES` (empty by
default — see the docstring there for how to add one from
`~/.cshell2/config.py`).

botocore treats an enum as documentation and does not reject a value
absent from it, so an undeclared category reaches the service and the
service decides. `jobs.category_not_offered` classifies the resulting
`ValidationException` as "this endpoint does not have that category" and
reports the set once as a compact note, rather than one stderr line per
category on every `jobs list`. What remains is the wasted call: an
undeclared category is still *tried*, so `list` / `describe` / `watch`
spend one extra round-trip per such category per pass, and the job-name
completer one per typed token. Caching keeps the completer cost off the
keystroke path but does not remove it. A category that graduates into the
model stops costing anything, with no code change.

## Python commands cannot report an exit status

A `@registry.command` handler's return value is ignored.
`PythonCommandSlot._compute_exit_code` derives the status purely from
what escaped the handler: `SystemExit` → its code, `KeyboardInterrupt`
→ 130, any other exception → 1, clean return → 0. So a handler has no
way to say "I ran fine but the thing I was asked about failed", and
`my_cmd && other` treats an unhappy-but-clean run as success.

Raising `SystemExit` is not a workaround: per the entry above, the
redirect path re-raises it on the main thread and would take the shell
down.

Consequence for ported tools: every `awsut` leaf (`awsut.py` and
`_awsut_sagemaker/`, both wrapped in `_awsut_common.guard`) prints
`error: …` to stderr and returns normally where the standalone
`sm_jobs.py` / `sm_hub.py` scripts exited 1 or 2 —
and where the `make` targets `studio` replaces failed the build.
The distinction is visible to a human reading the output but not to
`&&` / `||`. Fixing this properly means threading a return value (or a
sentinel exception the slot understands) from `Command.invoke` through
`_run_python_command_sync` and `PythonCommandSlot` into
`_compute_exit_code` — worth doing, but it changes the contract for
every Python command, not just these.
