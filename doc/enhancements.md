# Enhancement Ideas

A living document for cshell2 enhancement ideas — features that would be
nice to have but aren't yet implemented. Each entry should be enough for a
future implementer (or design discussion) to pick up cold; flesh out
sections as the idea matures. Once an idea ships, either delete it or move
it under a "Shipped" subsection with the commit that landed it.

For known limitations of existing features, see [limitations.md](limitations.md).

## Pipeline decorators — follow-up items

The decorator feature itself is shipped and documented in
[decorators.md](decorators.md). The open follow-up work below stays
here in enhancements.md until each item lands.

- **Stacking** (`@time @watch {ls}`) — the parser currently peels one
  decorator. Loop in `_extract_decorator_prefix` and chain calls in
  dispatch.
- **Outer sequencing after a decorator scope** (`@deco {...} ; pwd`,
  `@deco {...} && other`) — currently rejected with a clear error.
  Allowing it means letting the outer-sequence parser treat the
  decorator-stage as one statement; the parser already isolates the
  decorator scope so the additional change is small.
- **More built-ins** — `@time`, `@retry`, `@quiet`, and `@bg` are
  shipped (alongside `@watch`).  Future candidates: `@confirm`
  (prompt before running) and `@nice -n N` (process-priority wrapper).
- **Reload integration** — `reload` should call
  `decorator_registry.clear_user_decorators()` once user decorators
  start landing in `~/.cshell2/decorators/`.
- **Slot-aware `@watch`** — route long-running decorator bodies
  through `PythonCommandSlot` so `Ctrl+]` backgrounding works the
  same as for regular Python commands.
- **`@watche ls` typo suggestions** — we own the lookup so suggesting
  `@watch` is a small, low-risk follow-up.
- **History** — `@watch ls` is stored in history as written, not
  per-iteration. Almost certainly the right default; just hasn't
  been pinned with a test.
- **Redirects bound to the decorator vs. the body** —
  `@time {make} > build.log` currently binds the redirect to the
  body's only stage, so `@time`'s own timing line would print to
  the terminal rather than land in the file. Likely correct; codify
  with a test once `@time` ships.

## Architectural follow-ups

The features have shipped (Python pipelines, decorators, `@bg`,
passthrough subprocesses, sub-command tree, cross-platform terminal),
but the layering hasn't fully caught up to them.  None of the items
below is breaking anything; each improves cohesion, makes the slot
subsystem testable in isolation, and makes the parser/executor
boundary explicit. They can land independently and in any order.

For background, see the "Known structural smells" section at the end
of [architecture.md](architecture.md).

- **Extract `slots.py` (or `slots/` package).** Move
  `_StdoutProxy`, `_NullBuffer`, `_PyStageHandle`,
  `PythonCommandSlot`, `PipelineSlot`, the three `_ThreadLocal*`
  stdio routers, `_dup_threadlocal_override_fd`, the
  `_current_slot` / `_in_pipeline` thread-locals, and the
  `passthrough_run` / `passthrough_poll_key` /
  `passthrough_input` free functions out of `shell.py` into one
  module. Roughly halves `shell.py`. The free functions become
  natural top-level exports of the slot module rather than
  reaching into module-private thread-locals from `shell.py`.
  *Risk:* moderate — the slot needs a callback into the shell to
  re-execute a pipeline (for `@bg` / `Pipeline.run` re-entry); a
  small `slots.set_pipeline_runner(callable)` hook (or
  `ExecutionEnvironment` — see below) handles that cleanly.

- **Extract a `dispatch.py` (pipeline executor) from `shell.py`.**
  A `PipelineExecutor` class owning `_execute`, `_execute_pipeline`,
  `_execute_stage`, `_start_python_stage_thread`,
  `_start_decorator_stage_thread`, `_execute_decorator_stage`,
  `_run_python_command_sync`, `_execute_external*`,
  `_tokenize_stage`, `_expand_alias`, `_pipeline_python_command`,
  `_pipeline_external_argv`, plus the redirect-resolution code.
  This is also the module that should *own* the `Pipeline.run`
  executor — `Pipeline.run` becomes `executor.run(pipeline)`
  injected via constructor. Big payoff: the redirect path becomes
  testable without spinning up a full `Shell`. *Risk:* high — most
  of the runtime state lives here; needs a clear value object
  (`ShellEnvironment`) carrying the registries and the context
  manager.

- **Replace the four module-global setters with one
  `ExecutionEnvironment` interface.** Today `Shell.__init__`
  calls four parallel registration hooks:
  `pipeline.set_pipeline_executor`,
  `decorators.set_background_runner`,
  `pipeline.set_decorator_value_flag_lookup`, plus the implicit
  `_current_slot` / `_in_pipeline` thread-locals consumed by the
  free `passthrough_*` functions. Replace with a small Protocol
  carrying `run_pipeline(pipeline)`,
  `run_in_background(pipeline, name)`, `current_slot()`,
  `in_pipeline()`, `decorator_value_flag(name, flag)`. Carry it
  via `contextvars` so two `Shell` instances can coexist.
  *Risk:* low–moderate — the wiring exists; this is renaming and
  consolidation.

- **Unify the two raw-mode forwarding loops.**
  `_enter_forwarding_mode` (PTY-backed `ProcessSlot`) and
  `_enter_python_forwarding_mode` (`PythonCommandSlot`) duplicate
  ~80% of their logic — termios snapshot/restore, SIGWINCH+SIGINT
  install, `\x1d` interception, byte forwarding. Factor into one
  `ForwardingLoop` taking a slot interface
  (`is_alive` / `write_stdin` / `kill` / `resize` /
  `on_input_request()`). `ProcessSlot` returns `None` from
  `on_input_request`; `PythonCommandSlot` returns its
  passthrough-input coordination object. *Risk:* low — surface
  is small; cuts ~100 lines and removes a bug class (any fix
  today must be applied twice).

- **`_open_redirects(stage)` helper in `pipeline.py`.** The
  redirect-open code is duplicated in `_execute_pipeline` and
  `_execute_stage` with subtly different sentinels
  (`subprocess.STDOUT` vs the string `"stdout"` for `2>&1`).
  Pull both call sites onto one helper, single sentinel. *Risk:*
  trivial.

- **Move the Ctrl+] context-switch UI into a `switcher.py`** (or
  back into `context.py`). `_show_switch_menu`, `_resume_pty_slot`,
  `_handle_switch`, `_NEW_CTX_SENTINEL`, `_running_contexts`,
  `_confirm_exit` are UI-over-`ContextManager`. *Risk:* low —
  almost a pure move; `_handle_switch` already returns a sentinel
  to `lineedit`, which is a clean boundary. Today
  `_resume_pty_slot` reaches into `slot.terminal_modes`
  (process.py internals) — make this a method on the slot.

- **Promote `pipeline._split_on_operators` to a public name.**
  It is imported from `shell.py` (cross-module use of a leading
  underscore) for both completion-stage isolation and decorator-
  prefix remainder validation. Drop the underscore or move to
  `parsing.py`. *Risk:* trivial (rename).

- **Make `PipelineSlot.__init__` call `super().__init__`
  cleanly.** Today it bypasses the parent's init and re-creates
  attributes by hand, which is brittle when
  `PythonCommandSlot.__init__` changes. Introduce an
  `_init_common()` on the base, or a small `WorkUnit` strategy so
  the subclass only specifies what it runs.

- **Reload integration for user decorators.** Once
  `~/.cshell2/decorators/` lands, `reload` should call
  `decorator_registry.clear_user_decorators()` (the method
  already exists). Already noted under "Pipeline decorators —
  follow-up items"; mentioned here for completeness.

These are sequenced from highest payoff (shrinks `shell.py` the
most, exposes the cleanest public interface) to lowest. Doing the
first two together — `slots.py` + `dispatch.py` — collapses
`shell.py` to roughly the REPL-and-built-ins module its name
implies (~600 lines), which is also what the architecture diagrams
in CLAUDE.md and architecture.md already promise.
