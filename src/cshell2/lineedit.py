"""DIY line editor with history and TAB completion — no prompt_toolkit."""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Callable

from . import terminal
from .completion import Completion

SWITCH_SENTINEL = "\x1d__SWITCH__"
CONTEXT_CHANGED_SENTINEL = "\x1d__CHANGED__"

_NEEDS_QUOTING = re.compile(r"[^\w@%+=:,./~-]")


def _shell_quote(s: str) -> str:
    """Like shlex.quote but treats ~ as safe (common in home-dir paths like ~/foo)."""
    if not s:
        return "''"
    if not _NEEDS_QUOTING.search(s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"

_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def _wcswidth(s: str) -> int:
    """Terminal display width of s (wide/fullwidth chars count as 2 columns).

    Combining/format characters (Unicode category Mn, Me, Cf) are zero-width
    and checked BEFORE east_asian_width so that NFD-decomposed characters like
    voiced katakana (e.g. ガ → カ + U+3099 combining dakuten) are not
    double-counted.  U+3099 has east_asian_width='W' in Python's unicodedata,
    but it is a combining mark and must be treated as zero-width.
    """
    w = 0
    for ch in s:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue  # zero-width combining / format char
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _visible_len(s: str) -> int:
    """Display width of s after stripping ANSI escape codes (wide chars count as 2)."""
    return _wcswidth(_ANSI_RE.sub("", s))


def _display_col_offset(prefix: str, completions: list[Completion]) -> int:
    """Return how many terminal columns of prefix appear at the start of every display value.

    The picker should open this many columns to the LEFT of the caret so that
    the candidate text aligns with the already-typed partial token.
    E.g. prefix="doc/co", displays=["completion.md","context.md"] → 2 ("co").
    Wide chars in the prefix count as 2 columns each.
    """
    for start in range(len(prefix) + 1):
        suffix = prefix[start:]
        if all(c.display.startswith(suffix) for c in completions):
            return _wcswidth(suffix)
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
        line = line.rstrip()
        if not line:
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

GetCompletionsFn = Callable[[str], tuple[list[Completion], str, str]]
GetArgInfoFn = Callable[[str, int], str | None]


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
        switch_fn: Callable[[], None] | None = None,
        get_arg_info: GetArgInfoFn | None = None,
    ):
        self._history = history
        self._get_completions = get_completions
        self._get_prompt = get_prompt
        self._switch_fn = switch_fn
        self._get_arg_info = get_arg_info

        self._buf = ""
        self._cursor = 0
        self._hist_idx = 0
        self._saved_buf = ""
        self._cols = 80
        self._lines = 24
        self._prompt_str = ""
        self._prompt_len = 0
        self._cursor_row = 0  # rows below render-top where cursor sits
        self._add_to_history = True
        # VSCode integrated terminal does not reflow content on resize;
        # cursor stays at the same row (clamped column). Detect it so we
        # re-render explicitly instead of relying on terminal reflow.
        self._terminal_reflows = os.environ.get("TERM_PROGRAM", "") != "vscode"
        self._hint: str = ""  # transient hint shown after TAB; cleared on next keypress
        self._status_bar_visible: bool = False  # whether the status bar is currently showing something

    def add_to_history(self, line: str) -> None:
        """Add *line* to history from outside the editor (e.g. after joining continuation lines)."""
        self._history.add(line)

    def prompt(self, prompt_str: str | None = None, add_to_history: bool = True) -> str:
        """Read one line.

        Args:
            prompt_str: If given, display this string instead of calling _get_prompt().
                        Useful for continuation prompts (e.g. ``"> "``).
            add_to_history: When False the entered line is *not* added to history.
                            Use this when the caller will join multiple lines and add
                            the combined command to history itself.
        """
        self._buf = ""
        self._cursor = 0
        self._hist_idx = 0
        self._saved_buf = ""
        self._add_to_history = add_to_history
        self._prompt_str = prompt_str if prompt_str is not None else self._get_prompt()
        self._prompt_len = _visible_len(self._prompt_str)

        fd = sys.stdin.fileno()
        old_attrs = terminal.get_mode(fd)
        old_sigwinch = terminal.install_resize_handler(self._on_resize)

        try:
            self._update_cols()
            self._cursor_row = 0
            terminal.set_raw(fd)
            self._redraw()

            while True:
                key = terminal.read_key(fd)
                result = self._handle_key(key, fd)
                if result is not None:
                    self._cursor = len(self._buf)
                    self._redraw()
                    self._clear_status_bar()
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return result
                self._redraw()
        except (EOFError, KeyboardInterrupt):
            self._clear_status_bar()
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            raise
        finally:
            terminal.restore_mode(fd, old_attrs)
            terminal.restore_resize_handler(old_sigwinch)

    # ── terminal size ────────────────────────────────────────────────────────

    def _update_cols(self) -> None:
        try:
            sz = os.get_terminal_size()
            self._cols = sz.columns
            self._lines = sz.lines
        except OSError:
            self._cols = 80
            self._lines = 24

    def _on_resize(self, _sig, _frame) -> None:
        old_cursor_row = self._cursor_row
        self._update_cols()
        cursor_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
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
        cursor_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
        total_char = self._prompt_len + _wcswidth(self._buf)

        # Go up to the render top, then clear to end of screen.
        if self._cursor_row > 0:
            sys.stdout.write(f"\033[{self._cursor_row}A")
        sys.stdout.write("\r\033[J")
        sys.stdout.write(self._prompt_str + self._buf)

        # Decide whether the status bar needs to render.
        from .tui import _statusbar
        statusbar_str: str | None = None
        if self._hint:
            statusbar_str = _statusbar(self._hint, "", self._cols)
        elif self._get_arg_info is not None:
            info = self._get_arg_info(self._buf, self._cursor)
            if info:
                statusbar_str = _statusbar(info, "", self._cols)

        # When the status bar will be drawn, ensure the bottom row is free. If
        # end-of-content sits on the terminal's last row, \n triggers a scroll
        # (raw mode keeps the column); the matching \033[A returns to the same
        # visual position. When not at the bottom, the pair is a visual no-op.
        if statusbar_str is not None:
            sys.stdout.write("\n\033[A")

        # Compute cursor position within the render for next resize.
        self._cursor_row = _pending_wrap_row(cursor_char, cols)

        # After writing the buffer the cursor is at the end of content.
        end_row = _pending_wrap_row(total_char, cols)

        # Navigate from end of content back to where the cursor belongs.
        rows_up = end_row - self._cursor_row
        if rows_up > 0:
            sys.stdout.write(f"\033[{rows_up}A")

        # Use CHA (absolute column) so the caret jumps to its final position in
        # one atomic move — `\r` followed by `\033[{N}C` flickers through col 0.
        cursor_col = _pending_wrap_col(cursor_char, cols)
        sys.stdout.write(f"\033[{cursor_col + 1}G")

        if statusbar_str is not None:
            sys.stdout.write("\0337")
            sys.stdout.write(f"\033[{self._lines};1H")
            sys.stdout.write(statusbar_str)
            sys.stdout.write("\0338")
            self._status_bar_visible = True
        elif self._status_bar_visible:
            sys.stdout.write("\0337")
            sys.stdout.write(f"\033[{self._lines};1H\033[2K")
            sys.stdout.write("\0338")
            self._status_bar_visible = False

        sys.stdout.flush()

    def _clear_status_bar(self) -> None:
        """Wipe the status bar from the bottom row before yielding the terminal.

        Called when prompt() is about to return so that command output (or the
        next prompt) doesn't have to fight with leftover status-bar pixels.
        """
        if not self._status_bar_visible:
            return
        sys.stdout.write("\0337")
        sys.stdout.write(f"\033[{self._lines};1H\033[2K")
        sys.stdout.write("\0338")
        sys.stdout.flush()
        self._status_bar_visible = False

    # ── input ────────────────────────────────────────────────────────────────

    def _handle_key(self, key: bytes, fd: int) -> str | None:
        """Return a result string to finish, or None to keep editing."""
        self._hint = ""  # any keypress dismisses the hint; TAB may re-set it

        # Enter
        if key in (b"\r", b"\n"):
            result = self._buf
            if self._add_to_history:
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
            if self._switch_fn is not None:
                needs_forward = self._do_inline_switch()
                return CONTEXT_CHANGED_SENTINEL if needs_forward else None
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

        # Ctrl+R — history search
        if key == b"\x12":
            self._history_search(fd)
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

        # UTF-8 multi-byte char (already fully assembled by terminal.read_key)
        if key[:1] >= b"\x80":
            ch = key.decode("utf-8", errors="replace")
            if ch.isprintable():
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

    # ── context switch ───────────────────────────────────────────────────────

    def _do_inline_switch(self) -> bool:
        """Run the context-switch picker inline, preserving the current buffer.

        Returns True if the new context has a running process (caller should
        exit prompt so the run loop can enter forwarding mode).
        """
        caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
        caret_row = _pending_wrap_row(caret_char, self._cols)
        end_row = _pending_wrap_row(self._prompt_len + _wcswidth(self._buf), self._cols)
        rows_above = end_row - caret_row + 1

        cols_from_end = _wcswidth(self._buf[self._cursor:])
        if cols_from_end > 0:
            sys.stdout.write(f"\033[{cols_from_end}C")
        sys.stdout.write("\n")
        sys.stdout.flush()

        assert self._switch_fn is not None
        needs_forward = self._switch_fn()

        # Picker cleanup left cursor at the anchor (col 0 of the blank line).
        # Move back up to the caret row so _redraw() can take over from there.
        # Skip the flush so the up-move batches with the redraw the caller
        # performs next — otherwise the terminal briefly renders the caret at
        # col 0 of the prompt row.
        sys.stdout.write(f"\033[{rows_above}A")

        # Prompt text may have changed after a context switch.
        self._prompt_str = self._get_prompt()
        self._prompt_len = _visible_len(self._prompt_str)

        return bool(needs_forward)

    # ── completion ───────────────────────────────────────────────────────────

    def _complete(self, fd: int) -> None:
        from .tui import InlinePicker

        buf_changed = False
        while True:
            # Redraw if a previous iteration modified the buffer (e.g. auto-applied a
            # flag), so the prompt reflects the new content before the next picker opens.
            if buf_changed:
                self._redraw()
                buf_changed = False

            completions, prefix, status_label = self._get_completions(self._buf[: self._cursor])

            if not completions:
                return

            # Arg-hint: the preceding flag needs a typed value (e.g. "-d N").
            # Show an informational hint below the buffer without opening a
            # picker or modifying the buffer — cleared by the next _redraw().
            if len(completions) == 1 and completions[0].is_arg_hint:
                hint = completions[0]
                if hint.description:
                    self._hint = f"{hint.value} <{hint.arg_hint}>: {hint.description}"
                else:
                    self._hint = f"{hint.value} <{hint.arg_hint}>"
                return

            # Single value-taking option: auto-apply then re-run the loop.
            # The next iteration will either open a value picker (when a
            # value_completer is registered) or set self._hint (is_arg_hint).
            if (len(completions) == 1
                    and completions[0].multi_select
                    and completions[0].arg_hint):
                self._apply(completions[0], prefix)
                buf_changed = True
                continue

            if len(completions) == 1:
                self._apply(completions[0], prefix)
                if completions[0].arg_hint:
                    self._prompt_for_arg(completions[0])
                return

            # Multi-select options picker.
            if all(c.multi_select for c in completions):
                self._complete_multi(completions, prefix, status_label)
                return

            # Move to the end of the visible content, then go one line down.
            # The prompt line stays visible above the picker during interaction.
            caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
            caret_col = _pending_wrap_col(caret_char, self._cols)
            caret_row = _pending_wrap_row(caret_char, self._cols)
            end_row = _pending_wrap_row(self._prompt_len + _wcswidth(self._buf), self._cols)
            rows_above = end_row - caret_row + 1
            display_offset = _display_col_offset(prefix, completions)
            col = caret_col - display_offset

            cols_from_end = _wcswidth(self._buf[self._cursor:])
            if cols_from_end > 0:
                sys.stdout.write(f"\033[{cols_from_end}C")
            sys.stdout.write("\n")
            sys.stdout.flush()

            buf_at_tab = self._buf[: self._cursor]
            caret_char_at_tab = caret_char

            def refresh(typed: str) -> tuple[list[Completion], int]:
                new_completions, new_prefix, _ = self._get_completions(buf_at_tab + typed)
                new_caret_col = _pending_wrap_col(
                    caret_char_at_tab + len(typed), self._cols  # typed is always ASCII
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
                status_label=status_label,
            )
            selected = picker.run()

            # Picker cleanup leaves cursor at the anchor (col `picker._col` of
            # the first blank row) WITHOUT flushing. Move up to the caret row
            # and skip the flush so these bytes batch with the next render
            # (either the reopened picker's _reserve or _redraw on return) —
            # otherwise the terminal briefly shows the caret at the start of
            # the token being completed.
            sys.stdout.write(f"\033[{rows_above}A")

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

    def _complete_multi(self, completions: list[Completion], prefix: str, status_label: str = "") -> None:
        """Run the multi-select options picker."""
        from .tui import InlineMultiPicker

        caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
        caret_col = _pending_wrap_col(caret_char, self._cols)
        caret_row = _pending_wrap_row(caret_char, self._cols)
        end_row = _pending_wrap_row(self._prompt_len + _wcswidth(self._buf), self._cols)
        rows_above = end_row - caret_row + 1

        cols_from_end = _wcswidth(self._buf[self._cursor:])
        if cols_from_end > 0:
            sys.stdout.write(f"\033[{cols_from_end}C")
        sys.stdout.write("\n")
        sys.stdout.flush()

        picker = InlineMultiPicker(
            completions,
            display_fn=lambda c: f"{c.display or c.value} <{c.arg_hint}>" if c.arg_hint else (c.display or c.value),
            meta_fn=lambda c: c.description,
            max_height=12,
            rows_above=rows_above,
            caret_col=caret_col,
            status_label=status_label,
        )
        selected = picker.run()

        # No flush — let these bytes batch with the next _redraw so the caret
        # doesn't briefly land at the picker's anchor column on the prompt row.
        sys.stdout.write(f"\033[{rows_above}A")

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

        # For each arg-taking flag, insert it then handle its value:
        #   • flags with a value completer (e.g. -C DIR) → picker via _prompt_for_arg
        #   • hint-only flags (e.g. -j N)               → hint line, return to user
        for opt in arg_sel:
            sep = " " if self._cursor > 0 and self._buf[self._cursor - 1] != " " else ""
            ins = f"{sep}{opt.value} "
            self._buf = self._buf[: self._cursor] + ins + self._buf[self._cursor :]
            self._cursor += len(ins)

            value_comps, _, _ = self._get_completions(self._buf[: self._cursor])
            has_value_picker = any(
                not c.multi_select and not c.is_arg_hint for c in value_comps
            )

            if has_value_picker:
                # Value completer available: open picker, then continue to next flag.
                if not self._prompt_for_arg(opt):
                    break
            else:
                # Hint-only: show hint below and hand control back to the user.
                # They type the value directly; any remaining flags wait for next TAB.
                hint_comp = next((c for c in value_comps if c.is_arg_hint), None)
                if hint_comp:
                    if hint_comp.description:
                        self._hint = f"{hint_comp.value} <{hint_comp.arg_hint}>: {hint_comp.description}"
                    else:
                        self._hint = f"{hint_comp.value} <{hint_comp.arg_hint}>"
                break

    def _history_search(self, fd: int) -> None:
        from .tui import InlinePicker

        entries = self._history.entries
        if not entries:
            return

        # Deduplicate, most recent first
        seen: set[str] = set()
        unique: list[str] = []
        for e in reversed(entries):
            if e not in seen:
                seen.add(e)
                unique.append(e)

        saved_buf = self._buf
        saved_cursor = self._cursor

        self._buf = ""
        self._cursor = 0
        self._redraw()

        # Move below the (now empty) prompt line
        sys.stdout.write("\n")
        sys.stdout.flush()

        caret_col = _pending_wrap_col(self._prompt_len, self._cols)

        def refresh(typed: str) -> tuple[list[str], int]:
            if not typed:
                return unique, caret_col
            keywords = typed.lower().split()
            filtered = [e for e in unique if all(k in e.lower() for k in keywords)]
            return filtered, caret_col

        picker = InlinePicker(
            unique,
            display_fn=str,
            max_height=10,
            col=caret_col,
            initial_offset=0,
            rows_above=1,
            refresh_fn=refresh,
            value_fn=None,  # disable tab-complete inside the search picker
            status_label="history search",
        )
        selected = picker.run()

        # No flush — let the up-move batch with _redraw on return.
        sys.stdout.write("\033[1A")

        if selected is not None:
            self._buf = selected
            self._cursor = len(self._buf)
            self._hist_idx = 0
        else:
            self._buf = saved_buf
            self._cursor = saved_cursor

    def _raw_token_start(self) -> int:
        """Return the index in self._buf where the current raw token starts.

        Scans forward up to the cursor, tracking single- and double-quote
        state so that a token like ``'My Documents/'`` is treated as one unit.
        The returned index is the position of the first character of the last
        whitespace-delimited (but quote-aware) token before the cursor.
        """
        buf = self._buf[: self._cursor]
        last_start = 0
        i = 0
        while i < len(buf):
            c = buf[i]
            if c in (" ", "\t"):
                last_start = i + 1
                i += 1
            elif c in ("'", '"'):
                j = buf.find(c, i + 1)
                if j == -1:
                    break  # unclosed quote — rest is part of this token
                i = j + 1
            else:
                i += 1
        return last_start


        sys.stdout.flush()

    def _apply(self, completion: Completion, prefix: str) -> None:  # noqa: ARG002
        # Find where the raw token starts in the buffer.  We cannot use
        # len(prefix) here because shlex.split returns the *unquoted* length,
        # which differs from the raw length when the token is surrounded by
        # quotes (e.g. `'My Documents/'` is 16 raw chars but 14 unquoted).
        raw_start = self._raw_token_start()
        pre = self._buf[:raw_start]
        post = self._buf[self._cursor :]
        # Shell-quote the value if it contains whitespace or other characters
        # that shlex would split on (e.g. spaces in S3 keys or local filenames).
        # _shell_quote only adds quotes when necessary and treats ~ as safe so
        # that home-dir paths like ~/Desktop/ are not needlessly quoted.
        value = _shell_quote(completion.value)
        # Append a trailing space so the next argument can be typed immediately.
        # Skip when: (a) the value ends with "/" — a directory, where the user
        # may continue typing the path; (b) the value ends with "=" — a KEY=
        # completion where the user will continue typing the value; (c) post
        # already starts with whitespace.
        # arg_hint flags always get a space — _prompt_for_arg uses it as a separator.
        if completion.arg_hint:
            value = value + " "
        elif not value.endswith(("/", "=")) and not post[:1].isspace():
            value = value + " "
        self._buf = pre + value + post
        self._cursor = len(pre) + len(value)

    def _prompt_for_arg(self, opt: Completion) -> bool:
        """Show an inline prompt for opt's argument, insert the value, return False if cancelled.

        If the completion engine returns candidates for the current buffer state
        (i.e. a completer is registered for the flag's value), an InlinePicker is
        shown instead of the plain InlineArgPrompt text input.
        """
        from .tui import InlineArgPrompt, InlinePicker

        self._redraw()

        # Ask the completion engine what's available for this argument position.
        # Filter out multi_select entries (flag pickers) and is_arg_hint entries
        # (hint-only flags with no value completer) — only real value completions remain.
        raw_completions, prefix, _ = self._get_completions(self._buf[: self._cursor])
        completions = [c for c in raw_completions if not c.multi_select and not c.is_arg_hint]

        end_char = self._prompt_len + _wcswidth(self._buf)
        end_row = _pending_wrap_row(end_char, self._cols)
        end_col = _pending_wrap_col(end_char, self._cols)
        caret_row = self._cursor_row  # updated by _redraw()

        if opt.arg_hint and opt.description:
            _flag_label = f"{opt.value} <{opt.arg_hint}>: {opt.description}"
        elif opt.description:
            _flag_label = f"{opt.value}: {opt.description}"
        elif opt.arg_hint:
            _flag_label = f"{opt.value} <{opt.arg_hint}>"
        else:
            _flag_label = opt.value

        if not completions:
            # ── free-text fallback: InlineArgPrompt (original behaviour) ──────
            rows_to_end = end_row - caret_row
            if rows_to_end > 0:
                sys.stdout.write(f"\033[{rows_to_end}B")
            sys.stdout.write("\r")
            if end_col > 0:
                sys.stdout.write(f"\033[{end_col}C")
            sys.stdout.write("\n")
            sys.stdout.flush()

            arg_prompt = InlineArgPrompt(
                label=f"{opt.value} <{opt.arg_hint}>",
                description=opt.description,
                status_label=_flag_label,
            )
            value = arg_prompt.run()

            # InlineArgPrompt._cleanup() left the cursor at anchor (col 0 of the
            # prompt line, end_row + 1 below render-top). Move back to caret_row
            # without flushing so the up-move batches with _redraw on return.
            sys.stdout.write(f"\033[{end_row + 1 - caret_row}A")

            if value is None:
                return False
            self._buf = self._buf[: self._cursor] + value + self._buf[self._cursor :]
            self._cursor += len(value)
            return True

        # ── picker path: completions are available for the flag value ──────────
        # Loop mirrors _complete()'s while-True structure to handle tab-extend
        # (picker.reopen) and backspace (picker.apply_backspace).
        while True:
            caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
            caret_col = _pending_wrap_col(caret_char, self._cols)
            caret_row = _pending_wrap_row(caret_char, self._cols)
            end_char = self._prompt_len + _wcswidth(self._buf)
            end_row = _pending_wrap_row(end_char, self._cols)
            rows_above = end_row - caret_row + 1
            display_offset = _display_col_offset(prefix, completions)
            col = caret_col - display_offset

            # Move cursor to the end of the buffer content, then one line below.
            cols_from_end = _wcswidth(self._buf[self._cursor:])
            if cols_from_end > 0:
                sys.stdout.write(f"\033[{cols_from_end}C")
            sys.stdout.write("\n")
            sys.stdout.flush()

            buf_at_open = self._buf[: self._cursor]
            caret_char_at_open = caret_char

            def refresh(typed: str) -> tuple[list[Completion], int]:
                new_raw, new_prefix, _ = self._get_completions(buf_at_open + typed)
                new_completions = [c for c in new_raw if not c.multi_select]
                new_caret_col = _pending_wrap_col(
                    caret_char_at_open + len(typed), self._cols  # typed is always ASCII
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
                status_label=_flag_label,
            )
            selected = picker.run()

            # Picker cleanup left cursor at col `picker._col` of (end_row + 1).
            # Go back up to the caret row, but don't flush — let these bytes
            # batch with the next render so we don't briefly show the caret
            # at the picker's anchor column on the prompt row.
            sys.stdout.write(f"\033[{rows_above}A")

            if picker.reopen:
                # TAB was pressed inside the picker: extend the typed chars into
                # the buffer and reopen with a refreshed completion list.
                typed = picker._typed
                self._buf = self._buf[: self._cursor] + typed + self._buf[self._cursor :]
                self._cursor += len(typed)
                completions, prefix, _ = self._get_completions(self._buf[: self._cursor])
                completions = [c for c in completions if not c.multi_select]
                if not completions:
                    return True  # typed chars committed; no further completions
                if len(completions) == 1:
                    self._apply(completions[0], prefix)
                    return True  # single completion; close like _complete() does
                continue

            if picker.apply_backspace:
                # Backspace with nothing typed: remove the trailing space the flag
                # inserted and let the user continue editing freely.
                if self._cursor > 0:
                    self._buf = self._buf[: self._cursor - 1] + self._buf[self._cursor :]
                    self._cursor -= 1
                return True

            if selected is None:
                return False

            self._apply(selected, prefix)
            return True
