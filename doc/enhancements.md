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
- **More built-ins** (`@time`, `@retry`, `@quiet`, `@bg`) —
  each gets its own `cshell2/decorators/<name>.py` and a call to
  `enable(...)` in `_register_builtins`.
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
