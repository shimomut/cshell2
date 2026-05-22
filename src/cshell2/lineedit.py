"""DIY line editor with history and TAB completion — no prompt_toolkit."""

from __future__ import annotations

import os
import re
import select
import signal
import sys
import termios
import tty
from pathlib import Path
from typing import Callable

from .completion import Completion

SWITCH_SENTINEL = "\x1d__SWITCH__"

_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def _visible_len(s: str) -> int:
    """Length of s after stripping ANSI escape codes."""
    return len(_ANSI_RE.sub("", s))


def _display_col_offset(prefix: str, completions: list[Completion]) -> int:
    """Return how many chars of prefix appear at the start of every display value.

    The picker should open this many columns to the LEFT of the caret so that
    the candidate text aligns with the already-typed partial token.
    E.g. prefix="doc/co", displays=["completion.md","context.md"] → 2 ("co").
    """
    for start in range(len(prefix) + 1):
        suffix = prefix[start:]
        if all(c.display.startswith(suffix) for c in completions):
            return len(suffix)
    return 0


def _pending_wrap_row(char_count: int, cols: int) -> int:
    """Row offset below render-top where cursor sits after writing char_count visible chars.

    Writing exactly N*cols chars leaves the cursor in pending-wrap state on the
    last filled row (row N-1), not on the next row. N//cols would be off by one.
    """
    if char_count <= 0:
        return 0
    return (char_count - 1) // cols


def _pending_wrap_col(char_count: int, cols: int) -> int:
    """Column offset (from col 0) for the cursor after writing char_count visible chars.

    When the content exactly fills a row, the cursor sits at the rightmost column
    in pending-wrap state. cursor_char % cols would give 0 (wrong).
    """
    if char_count <= 0:
        return 0
    rem = char_count % cols
    return rem if rem != 0 else cols - 1


# ── History ──────────────────────────────────────────────────────────────────


class History:
    def __init__(self, path: Path):
        self._path = path
        self._entries: list[str] = []
        self._load()

    def _load(self) -> None:
        try:
            self._entries = [
                ln for ln in self._path.read_text().splitlines() if ln.strip()
            ]
        except FileNotFoundError:
            pass

    def add(self, line: str) -> None:
        if not line.strip():
            return
        if self._entries and self._entries[-1] == line:
            return
        self._entries.append(line)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    @property
    def entries(self) -> list[str]:
        return self._entries


# ── Line editor ───────────────────────────────────────────────────────────────

GetCompletionsFn = Callable[[str], tuple[list[Completion], str]]


class LineEditor:
    """
    Raw-mode line editor. Handles its own key dispatch, history, and TAB
    completion via InlinePicker. No prompt_toolkit involved.

    prompt() returns the entered line, SWITCH_SENTINEL on Ctrl+], or raises
    EOFError (Ctrl+D on empty line) or KeyboardInterrupt (Ctrl+C).
    """

    def __init__(
        self,
        history: History,
        get_completions: GetCompletionsFn,
        get_prompt: Callable[[], str],
    ):
        self._history = history
        self._get_completions = get_completions
        self._get_prompt = get_prompt

        self._buf = ""
        self._cursor = 0
        self._hist_idx = 0
        self._saved_buf = ""
        self._cols = 80
        self._prompt_str = ""
        self._prompt_len = 0
        self._cursor_row = 0  # rows below render-top where cursor sits
        # VSCode integrated terminal does not reflow content on resize;
        # cursor stays at the same row (clamped column). Detect it so we
        # re-render explicitly instead of relying on terminal reflow.
        self._terminal_reflows = os.environ.get("TERM_PROGRAM", "") != "vscode"

    def prompt(self) -> str:
        self._buf = ""
        self._cursor = 0
        self._hist_idx = 0
        self._saved_buf = ""
        self._prompt_str = self._get_prompt()
        self._prompt_len = _visible_len(self._prompt_str)

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)

        try:
            self._update_cols()
            self._cursor_row = 0
            tty.setraw(fd)
            signal.signal(signal.SIGWINCH, self._on_resize)
            self._redraw()

            while True:
                key = self._read_key(fd)
                result = self._handle_key(key, fd)
                if result is not None:
                    self._cursor = len(self._buf)
                    self._redraw()
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return result
                self._redraw()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            raise
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGWINCH, old_sigwinch)

    # ── terminal size ────────────────────────────────────────────────────────

    def _update_cols(self) -> None:
        try:
            self._cols = os.get_terminal_size().columns
        except OSError:
            self._cols = 80

    def _on_resize(self, _sig, _frame) -> None:
        old_cursor_row = self._cursor_row
        self._update_cols()
        cursor_char = self._prompt_len + self._cursor
        if self._terminal_reflows:
            # Terminal reflows content and moves the cursor to the correct
            # position in the new geometry; just update our tracking.
            self._cursor_row = _pending_wrap_row(cursor_char, self._cols)
        else:
            # Terminal doesn't reflow (e.g. VSCode). The cursor stays on the
            # same row (clamped to the new width), so go up old_cursor_row to
            # reach render-top, then clear and redraw.
            if old_cursor_row > 0:
                sys.stdout.write(f"\033[{old_cursor_row}A")
            sys.stdout.write("\r\033[J")
            self._cursor_row = 0
            self._redraw()

    # ── rendering ────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        """Rewrite the prompt and buffer, handling multi-line wrapping."""
        cols = self._cols
        cursor_char = self._prompt_len + self._cursor
        total_char = self._prompt_len + len(self._buf)

        # Go up to the render top, then clear to end of screen.
        if self._cursor_row > 0:
            sys.stdout.write(f"\033[{self._cursor_row}A")
        sys.stdout.write("\r\033[J")
        sys.stdout.write(self._prompt_str + self._buf)

        # Compute cursor position within the render for next resize.
        self._cursor_row = _pending_wrap_row(cursor_char, cols)

        # Navigate from end of content back to where the cursor belongs.
        end_row = _pending_wrap_row(total_char, cols)
        rows_up = end_row - self._cursor_row
        if rows_up > 0:
            sys.stdout.write(f"\033[{rows_up}A")
        cursor_col = _pending_wrap_col(cursor_char, cols)
        sys.stdout.write("\r")
        if cursor_col > 0:
            sys.stdout.write(f"\033[{cursor_col}C")

        sys.stdout.flush()

    # ── input ────────────────────────────────────────────────────────────────

    def _read_key(self, fd: int) -> bytes:
        data = os.read(fd, 1)
        if data == b"\x1b":
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                data += os.read(fd, 8)
        return data

    def _handle_key(self, key: bytes, fd: int) -> str | None:
        """Return a result string to finish, or None to keep editing."""

        # Enter
        if key in (b"\r", b"\n"):
            result = self._buf
            self._history.add(result)
            return result

        # Ctrl+D — EOF if buffer empty
        if key == b"\x04":
            if not self._buf:
                raise EOFError
            return None

        # Ctrl+C
        if key == b"\x03":
            self._buf = ""
            self._cursor = 0
            raise KeyboardInterrupt

        # Ctrl+] — context switch
        if key == b"\x1d":
            return SWITCH_SENTINEL

        # TAB — completion
        if key == b"\x09":
            self._complete(fd)
            return None

        # Backspace
        if key in (b"\x7f", b"\x08"):
            if self._cursor > 0:
                self._buf = self._buf[: self._cursor - 1] + self._buf[self._cursor :]
                self._cursor -= 1
            return None

        # Ctrl+W — delete word before cursor
        if key == b"\x17":
            i = self._cursor
            while i > 0 and self._buf[i - 1] == " ":
                i -= 1
            while i > 0 and self._buf[i - 1] != " ":
                i -= 1
            self._buf = self._buf[:i] + self._buf[self._cursor :]
            self._cursor = i
            return None

        # Ctrl+K — delete to end of line
        if key == b"\x0b":
            self._buf = self._buf[: self._cursor]
            return None

        # Ctrl+U — delete to beginning
        if key == b"\x15":
            self._buf = self._buf[self._cursor :]
            self._cursor = 0
            return None

        # Ctrl+A / Home
        if key in (b"\x01", b"\x1b[H", b"\x1b[1~"):
            self._cursor = 0
            return None

        # Ctrl+E / End
        if key in (b"\x05", b"\x1b[F", b"\x1b[4~"):
            self._cursor = len(self._buf)
            return None

        # Ctrl+L — clear screen
        if key == b"\x0c":
            sys.stdout.write("\033[2J\033[H")
            return None

        # Ctrl+B / Left arrow
        if key in (b"\x02", b"\x1b[D"):
            if self._cursor > 0:
                self._cursor -= 1
            return None

        # Ctrl+F / Right arrow
        if key in (b"\x06", b"\x1b[C"):
            if self._cursor < len(self._buf):
                self._cursor += 1
            return None

        # Alt+B — move word left
        if key == b"\x1bb":
            i = self._cursor
            while i > 0 and self._buf[i - 1] == " ":
                i -= 1
            while i > 0 and self._buf[i - 1] != " ":
                i -= 1
            self._cursor = i
            return None

        # Alt+F — move word right
        if key == b"\x1bf":
            i = self._cursor
            n = len(self._buf)
            while i < n and self._buf[i] == " ":
                i += 1
            while i < n and self._buf[i] != " ":
                i += 1
            self._cursor = i
            return None

        # Up arrow — history back
        if key in (b"\x1b[A", b"\x10"):
            self._hist_back()
            return None

        # Down arrow — history forward
        if key in (b"\x1b[B", b"\x0e"):
            self._hist_fwd()
            return None

        # Printable ASCII
        if len(key) == 1 and 0x20 <= key[0] < 0x7F:
            ch = key.decode()
            self._buf = self._buf[: self._cursor] + ch + self._buf[self._cursor :]
            self._cursor += 1
            return None

        # UTF-8 multi-byte (first byte >= 0xC0)
        if len(key) == 1 and key[0] >= 0xC0:
            rest_len = (
                1 if key[0] < 0xE0 else 2 if key[0] < 0xF0 else 3
            )
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                key += os.read(fd, rest_len)
            ch = key.decode("utf-8", errors="replace")
            self._buf = self._buf[: self._cursor] + ch + self._buf[self._cursor :]
            self._cursor += 1
            return None

        return None

    # ── history ──────────────────────────────────────────────────────────────

    def _hist_back(self) -> None:
        entries = self._history.entries
        if not entries:
            return
        if self._hist_idx == 0:
            self._saved_buf = self._buf
        if self._hist_idx < len(entries):
            self._hist_idx += 1
            self._buf = entries[-self._hist_idx]
            self._cursor = len(self._buf)

    def _hist_fwd(self) -> None:
        if self._hist_idx == 0:
            return
        self._hist_idx -= 1
        if self._hist_idx == 0:
            self._buf = self._saved_buf
        else:
            self._buf = self._history.entries[-self._hist_idx]
        self._cursor = len(self._buf)

    # ── completion ───────────────────────────────────────────────────────────

    def _complete(self, fd: int) -> None:
        from .tui import InlinePicker

        while True:
            completions, prefix = self._get_completions(self._buf[: self._cursor])

            if not completions:
                return

            if len(completions) == 1:
                self._apply(completions[0], prefix)
                if completions[0].arg_hint:
                    self._prompt_for_arg(completions[0])
                return

            # Multi-select options picker.
            if all(c.multi_select for c in completions):
                self._complete_multi(completions, prefix)
                return

            # Move to the end of the visible content, then go one line down.
            # The prompt line stays visible above the picker during interaction.
            caret_char = self._prompt_len + self._cursor
            caret_col = _pending_wrap_col(caret_char, self._cols)
            caret_row = _pending_wrap_row(caret_char, self._cols)
            end_row = _pending_wrap_row(self._prompt_len + len(self._buf), self._cols)
            rows_above = end_row - caret_row + 1
            display_offset = _display_col_offset(prefix, completions)
            col = caret_col - display_offset

            chars_from_end = len(self._buf) - self._cursor
            if chars_from_end > 0:
                sys.stdout.write(f"\033[{chars_from_end}C")
            sys.stdout.write("\n")
            sys.stdout.flush()

            buf_at_tab = self._buf[: self._cursor]

            def refresh(typed: str) -> tuple[list[Completion], int]:
                new_completions, new_prefix = self._get_completions(buf_at_tab + typed)
                new_caret_col = _pending_wrap_col(
                    self._prompt_len + self._cursor + len(typed), self._cols
                )
                new_col = new_caret_col - _display_col_offset(new_prefix, new_completions)
                return new_completions, new_col

            picker = InlinePicker(
                completions,
                display_fn=lambda c: c.display or c.value,
                meta_fn=lambda c: c.description,
                max_height=10,
                col=col,
                initial_offset=display_offset,
                rows_above=rows_above,
                refresh_fn=refresh,
                value_fn=lambda c: c.value,
                completion_prefix=prefix,
                reopen_when=lambda items: bool(items) and all(c.multi_select for c in items),
            )
            selected = picker.run()

            # Picker cleanup leaves cursor at the anchor row (first blank line).
            # Move up rows_above lines to reach the caret row, then let _redraw
            # handle the rest.
            sys.stdout.write(f"\033[{rows_above}A")
            sys.stdout.flush()

            if picker.reopen:
                # TAB-complete typed chars; commit to buffer and reopen at new position.
                typed = picker._typed
                self._buf = self._buf[: self._cursor] + typed + self._buf[self._cursor :]
                self._cursor += len(typed)
                continue

            if picker.apply_backspace:
                # Backspace with no picker-typed chars: delete one buffer char and close.
                if self._cursor > 0:
                    self._buf = self._buf[: self._cursor - 1] + self._buf[self._cursor :]
                    self._cursor -= 1
                return

            if selected is not None:
                self._apply(selected, prefix)
            return

    def _complete_multi(self, completions: list[Completion], prefix: str) -> None:
        from .tui import InlineMultiPicker

        caret_char = self._prompt_len + self._cursor
        caret_col = _pending_wrap_col(caret_char, self._cols)
        caret_row = _pending_wrap_row(caret_char, self._cols)
        end_row = _pending_wrap_row(self._prompt_len + len(self._buf), self._cols)
        rows_above = end_row - caret_row + 1

        chars_from_end = len(self._buf) - self._cursor
        if chars_from_end > 0:
            sys.stdout.write(f"\033[{chars_from_end}C")
        sys.stdout.write("\n")
        sys.stdout.flush()

        picker = InlineMultiPicker(
            completions,
            display_fn=lambda c: f"{c.display or c.value} <{c.arg_hint}>" if c.arg_hint else (c.display or c.value),
            meta_fn=lambda c: c.description,
            max_height=12,
            rows_above=rows_above,
            caret_col=caret_col,
        )
        selected = picker.run()

        sys.stdout.write(f"\033[{rows_above}A")
        sys.stdout.flush()

        if not selected:
            return

        bool_sel = [c for c in selected if not c.arg_hint]
        arg_sel = [c for c in selected if c.arg_hint]

        # Replace the prefix and insert combined boolean flags.
        pre = self._buf[: self._cursor - len(prefix)]
        post = self._buf[self._cursor :]
        short = [c for c in bool_sel if c.combinable]
        long_bool = [c for c in bool_sel if not c.combinable]
        parts: list[str] = []
        if short:
            parts.append("-" + "".join(c.value[1:] for c in short))
        parts.extend(c.value for c in long_bool)
        bool_str = " ".join(parts)
        self._buf = pre + bool_str + post
        self._cursor = len(pre) + len(bool_str)

        # For each arg-taking option, insert the flag then prompt for its value.
        for opt in arg_sel:
            sep = " " if self._cursor > 0 and self._buf[self._cursor - 1] != " " else ""
            ins = f"{sep}{opt.value} "
            self._buf = self._buf[: self._cursor] + ins + self._buf[self._cursor :]
            self._cursor += len(ins)
            if not self._prompt_for_arg(opt):
                break

    def _apply(self, completion: Completion, prefix: str) -> None:
        pre = self._buf[: self._cursor - len(prefix)]
        post = self._buf[self._cursor :]
        # Append a trailing space for arg-taking options so _prompt_for_arg
        # can insert the value immediately after without an extra separator.
        value = completion.value + (" " if completion.arg_hint else "")
        self._buf = pre + value + post
        self._cursor = len(pre) + len(value)

    def _prompt_for_arg(self, opt: Completion) -> bool:
        """Show an inline prompt for opt's argument, insert the value, return False if cancelled."""
        from .tui import InlineArgPrompt

        self._redraw()

        # Navigate from the caret to one line below the end of the buffer.
        end_char = self._prompt_len + len(self._buf)
        end_row = _pending_wrap_row(end_char, self._cols)
        end_col = _pending_wrap_col(end_char, self._cols)
        caret_row = self._cursor_row  # updated by _redraw()

        rows_to_end = end_row - caret_row
        if rows_to_end > 0:
            sys.stdout.write(f"\033[{rows_to_end}B")
        sys.stdout.write("\r")
        if end_col > 0:
            sys.stdout.write(f"\033[{end_col}C")
        sys.stdout.write("\n")
        sys.stdout.flush()

        arg_prompt = InlineArgPrompt(label=f"{opt.value} <{opt.arg_hint}>", description=opt.description)
        value = arg_prompt.run()

        # InlineArgPrompt._cleanup() left the cursor at anchor (col 0 of the prompt
        # line, which is end_row + 1 below render-top). Move back to caret_row.
        sys.stdout.write(f"\033[{end_row + 1 - caret_row}A")
        sys.stdout.flush()

        if value is None:
            return False

        self._buf = self._buf[: self._cursor] + value + self._buf[self._cursor :]
        self._cursor += len(value)
        return True
