# Terminal Resize Handling

## The Problem

When the terminal window is resized, the OS sends `SIGWINCH` to the process.
At that point the shell is blocked in `_read_key()`, waiting for user input. The
visible prompt may now be misaligned: too wide, truncated, or wrapped
differently than the new terminal width requires.

The shell needs to:
1. Learn the new terminal width (`_cols`).
2. Update `_cursor_row` — the number of rows below the prompt's render-top
   where the cursor currently sits — so the next `_redraw()` navigates
   correctly.
3. Possibly re-render the prompt if the terminal did not do so itself.

## Two Terminal Behaviors

Terminal emulators differ fundamentally in how they handle resize:

### Reflow terminals (macOS Terminal.app, iTerm2, …)

When the window narrows, the terminal **reflows** its scroll buffer: every line
is re-wrapped to fit the new width, and the cursor is moved to follow its
logical content position.

After SIGWINCH:
- The prompt on screen is already visually correct (re-wrapped by the terminal).
- The cursor is at `cursor_char // new_cols` rows below the prompt's
  render-top, matching the new geometry.

**Strategy**: just update `_cursor_row` to match the new geometry. No
re-render is needed; the terminal already did it.

```python
self._cursor_row = _pending_wrap_row(cursor_char, self._cols)
```

### Non-reflow terminals (VSCode integrated terminal)

When the window narrows, the terminal **truncates** lines at the new width and
**clamps** the cursor to the same row (column clamped to the new right edge).
It does not reflow or move the cursor to a different row.

After SIGWINCH:
- The prompt on screen is visually truncated (wrong).
- The cursor is still on the same row it was before (`old_cursor_row` rows
  below render-top), just with the column clamped.

**Strategy**: navigate up `old_cursor_row` rows to reach render-top, clear to
end of screen, and re-render the prompt.

```python
if old_cursor_row > 0:
    sys.stdout.write(f"\033[{old_cursor_row}A")
sys.stdout.write("\r\033[J")
self._cursor_row = 0
self._redraw()
```

## Why Not a Single Unified Strategy?

The two behaviors are fundamentally incompatible:

| | Cursor after SIGWINCH | Correct `rows_up` to render-top |
|---|---|---|
| Reflow | Moved down N rows (content followed) | `cursor_char // new_cols` |
| Non-reflow | Same row, column clamped | `old_cursor_row` |

- Using the reflow formula on a non-reflow terminal goes **too many rows up**,
  eating the line above the prompt.
- Using the non-reflow formula on a reflow terminal goes **too few rows up**,
  leaving stale wrapped content above the redrawn prompt.

The only way to know the actual cursor row without terminal detection is to
issue a CPR (Cursor Position Report, `\033[6n`) query after resize and read the
terminal's response from stdin. This would be a universal solution, but it
requires a blocking stdin read inside the signal handler, which risks
interleaving with buffered user keystrokes — a reliability trade-off not worth
taking when `$TERM_PROGRAM` is a stable, documented identifier.

**Other shells face the same problem.** readline's `rl_resize_terminal()` does
an unconditional full redisplay, which works because readline tracks every
screen row's content in detail and issues targeted cursor movements. It also
contains terminal-specific `#ifdef` blocks. There is no clean universal
algorithm in common use.

## Detection

The terminal type is detected once at `LineEditor` construction:

```python
self._terminal_reflows = os.environ.get("TERM_PROGRAM", "") != "vscode"
```

VSCode sets `TERM_PROGRAM=vscode` in its integrated terminal. All other
terminals are assumed to reflow (which is the common behavior for
xterm-compatible emulators).

## Pending-Wrap Edge Case

Standard integer division (`cursor_char // cols`) overcounts the row by one
when `cursor_char` is exactly divisible by `cols`. Writing exactly N×cols
visible characters leaves the cursor in **pending-wrap state** on the last
filled row — it has not yet wrapped to the next row. Two helpers correct for
this:

```python
def _pending_wrap_row(char_count, cols):
    # Row offset below render-top where cursor sits.
    if char_count <= 0:
        return 0
    return (char_count - 1) // cols

def _pending_wrap_col(char_count, cols):
    # Column offset from col 0 (used after \r to position cursor).
    if char_count <= 0:
        return 0
    rem = char_count % cols
    return rem if rem != 0 else cols - 1
```

These are used everywhere cursor position is computed: in `_redraw()` for
`_cursor_row`, `end_row`, and `cursor_col`, and in `_on_resize()` for the
reflow branch.
