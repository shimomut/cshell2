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

## `awsut sagemaker studio watch` sees spaces and apps, but not the domain

The watch resolves `--domain` once and then polls two listings —
ListSpaces and ListApps — so the two resources that actually move during a
start or a stop are covered, and the cost per tick is O(1) in the size of
the domain rather than one DescribeApp per app. Three consequences:

- **The domain's own status is not tracked.** A domain going `Updating` or
  `Deleting` shows up only indirectly, as its spaces and apps changing or
  as a poll starting to fail. Watching a domain teardown would need a
  third call per tick (ListDomains) for a status that changes about twice
  in a domain's life.
- **`--max` bounds each listing, and the window can slide.** Both calls
  page to `--max` items of a newest-first listing, so in a domain holding
  more spaces or apps than the cap, a newly created one pushes the oldest
  out of the window — and the watch reports that as `no longer listed`
  when nothing was deleted. Raising `--max` or scoping with `--space`
  avoids it.
- **A space's deletion is inferred, not observed.** An app reaches
  `Deleted` and says so, because ListApps keeps returning it; a space just
  stops being listed, which is reported as a departure carrying its last
  known status.

## Desktop notifications can still fire for an interactive command

`notify.SKIP_COMMANDS` suppresses the obvious cases — editors, pagers,
process monitors, `ssh`, multiplexers, interactive sub-shells — by
matching the basename of the line's first word. It is a heuristic and it
misses in both directions:

- **A wrapper hides the interactive program.** `make menuconfig`,
  `git rebase -i`, `docker run -it ubuntu bash`, `kubectl exec -it`,
  `aws ssm start-session` all sit at a prompt for as long as the user
  wants and then "finish", earning a meaningless popup. The first word is
  the only thing examined, so no amount of tuning the set catches these.
- **The first word isn't the program.** `env FOO=1 vim` and `nohup vim`
  are not skipped; `sudo make` *is* skipped only if `sudo` were listed
  (it isn't, deliberately — `sudo make install` is worth reporting).
- **A skipped name can be a batch job.** `ssh host 'make release'` is a
  real long job and is silently skipped because it starts with `ssh`.

Deciding this properly means asking whether the process actually read from
the terminal — e.g. tracking whether the PTY slot ever received forwarded
stdin bytes, which `ProcessSlot` is in a position to know. That would
subsume the skip list for external commands (a `vim` session that took
keystrokes is self-evidently interactive) but not for Python commands or
`@bg` bodies. Until then: add to `notify.SKIP_COMMANDS` from
`~/.cshell2/config.py`, or `var notify=off` for a session spent in
interactive tools.

## Notification delivery is best-effort and mostly unverifiable from the shell

Every backend in `notify.py` is spawned as a subprocess with its output
discarded and every exception swallowed, so `command_done()` returning
`True` means "a notification was dispatched", never "the user saw it".
Specifically:

- **macOS attributes the notification to the terminal app**, because
  `osascript` posts on behalf of whatever host is running it. If
  notifications are denied for Terminal/iTerm2/VS Code in System
  Settings → Notifications, nothing appears and there is no error to
  detect. There is no way to ask for permission from a CLI, and no
  API to query the current grant.
- **The Windows toast path is untested on real hardware.** It is written
  against the documented WinRT `ToastText02` template and the PowerShell
  AppID, but the whole feature was developed and verified on macOS.
  Failure mode is a silent no-op (an unregistered AppID is dropped by the
  notification platform without an error).
- **Linux needs `notify-send` and a running notification daemon.** The
  binary being on `PATH` is what gets probed; a session with no daemon
  listening on the D-Bus name accepts the call and drops it.
- **The bell fallback is easy to miss** — many terminals have the audible
  bell disabled, in which case a fallback notification is nothing at all.

`set_notifier()` is the escape hatch: a custom backend (Slack, `ntfy.sh`,
`tmux display-message`) can be verified end-to-end by the user in a way the
built-in chain can't be.

## A backgrounded command that finishes while you are watching it is reported anyway

`Shell._notify_slot_done` stays silent when the slot's owning context *is*
the current one, on the grounds that a popup for something on screen is
noise. But the resume paths in `run()` call `_notify_resumed_done`
unconditionally for a slot that has already exited, so this sequence still
produces a notification:

1. `make -j8`, `Ctrl+]` to background it,
2. work in another context for two minutes,
3. `Ctrl+]` back to it *before* it finishes and watch the last of the build
   scroll past.

The reported duration correctly covers the whole run, but part of it was
spent in front of the user. Distinguishing "resumed and then finished" from
"finished unobserved" needs the slot to record when it was last activated,
which is more bookkeeping than the noise warrants today.

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
