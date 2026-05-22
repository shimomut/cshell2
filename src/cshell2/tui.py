"""DIY inline TUI widgets — no alternate screen, no scroll region."""

from __future__ import annotations

import os
import select
import signal
import sys
import termios
import tty
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


def _csi(code: str) -> str:
    return f"\033[{code}"


def _common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


class InlinePicker(Generic[T]):
    """
    Selectable list rendered inline below the current cursor position.

    Position is anchored with DECSC/DECRC (ESC 7 / ESC 8) at reserve time.
    On SIGWINCH the picker cancels immediately — redrawing after a resize is
    unreliable without an alternate screen, so the user just presses TAB again.
    """

    def __init__(
        self,
        items: list[T],
        display_fn: Callable[[T], str] = str,
        meta_fn: Callable[[T], str] | None = None,
        max_height: int = 10,
        col: int = 0,
        initial_offset: int = 0,
        rows_above: int = 1,
        refresh_fn: Callable[[str], tuple[list[T], int]] | None = None,
        value_fn: Callable[[T], str] | None = None,
        completion_prefix: str = "",
        reopen_when: Callable[[list[T]], bool] | None = None,
        min_width: int = 0,
        hide_cursor: bool = False,
    ):
        self._items = items
        self._display_fn = display_fn
        self._meta_fn = meta_fn
        self._max_height = max_height
        self._col = col
        self._initial_offset = initial_offset
        self._rows_above = rows_above
        self._refresh_fn = refresh_fn
        self._value_fn = value_fn
        self._completion_prefix = completion_prefix
        self._reopen_when = reopen_when
        self._min_width = min_width
        self._hide_cursor = hide_cursor
        self._typed = ""
        self.reopen = False          # set True when tab-complete typed chars; caller should reopen
        self.apply_backspace = False  # set True when backspace pressed with no typed chars

        self._selected = 0
        self._offset = 0
        self._cols = 80
        self._height = min(max_height, len(items))
        self._cancelled = False

    def run(self) -> T | None:
        if not self._items:
            return None

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)

        sig_r, sig_w = os.pipe()
        os.set_blocking(sig_w, False)
        old_wakeup_fd = signal.set_wakeup_fd(sig_w, warn_on_full_buffer=False)

        result: T | None = None
        if self._hide_cursor:
            sys.stdout.write("\x1b[?25l")
            sys.stdout.flush()
        try:
            self._update_size()
            self._reserve()
            tty.setraw(fd)
            signal.signal(signal.SIGWINCH, self._on_resize)
            self._render()

            while True:
                action = self._dispatch(self._read_key(fd, sig_r))
                if action == "accept":
                    result = self._items[self._selected] if self._items else None
                    break
                if action == "cancel":
                    break
                if action == "up":
                    self._move(-1)
                    self._render()
                elif action == "down":
                    self._move(1)
                    self._render()
                elif action == "tab_complete":
                    if self._handle_tab_complete():
                        break
                elif action == "backspace":
                    if self._handle_backspace():
                        break
                elif len(action) == 1:  # printable char
                    if self._handle_char(action):
                        break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            signal.set_wakeup_fd(old_wakeup_fd)
            os.close(sig_r)
            os.close(sig_w)
            self._cleanup()
            if self._hide_cursor:
                sys.stdout.write("\x1b[?25h")
                sys.stdout.flush()

        return result

    # ── size ────────────────────────────────────────────────────────────────

    def _update_size(self) -> None:
        sz = os.get_terminal_size()
        self._cols = sz.columns
        self._height = min(self._max_height, len(self._items), max(1, sz.lines - 3))

    def _on_resize(self, _sig, _frame) -> None:
        self._cancelled = True

    # ── drawing ─────────────────────────────────────────────────────────────

    def _reserve(self) -> None:
        """Create blank lines below cursor and save the top position with DECSC."""
        sys.stdout.write("\n" * self._height)
        col_move = f"\033[{self._col}C" if self._col > 0 else ""
        sys.stdout.write(_csi(f"{self._height}A") + "\r" + col_move + "\0337")
        sys.stdout.flush()

    def _render(self) -> None:
        """Restore to DECSC anchor, clear, draw rows, then place cursor at prompt caret."""
        visible = self._items[self._offset : self._offset + self._height]
        out: list[str] = ["\0338\r\033[J"]  # restore to anchor, clear to end

        panel_w = self._compute_panel_w()
        for i, item in enumerate(visible):
            out.append(self._format_row(item, selected=(i + self._offset == self._selected), row_index=i, panel_w=panel_w))
            if i < len(visible) - 1:
                out.append("\n")

        # Re-save anchor, then move cursor to prompt caret position.
        out.append("\0338\0337")
        if self._rows_above > 0:
            out.append(f"\033[{self._rows_above}A")
        caret_col_now = self._col + self._initial_offset + len(self._typed)
        out.append("\r")
        if caret_col_now > 0:
            out.append(f"\033[{caret_col_now}C")

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    _BG = "\033[48;5;236m"  # dark gray background for all rows

    def _scrollbar_char(self, row_index: int) -> str:
        n = len(self._items)
        thumb_start = self._offset * self._height // n
        thumb_end = max(thumb_start + 1, (self._offset + self._height) * self._height // n)
        if thumb_start <= row_index < thumb_end:
            return "\033[38;5;244m█\033[0m"
        return "\033[38;5;240m│\033[0m"

    def _compute_panel_w(self) -> int:
        """Width that fits all items (respecting min_width), bounded by available columns."""
        has_scrollbar = len(self._items) > self._height
        avail = max(1, self._cols - self._col - (1 if has_scrollbar else 0))
        max_w = self._min_width
        for item in self._items:
            label = self._display_fn(item)
            meta = self._meta_fn(item) if self._meta_fn else ""
            meta_clip = min(len(meta), min(20, avail // 4))
            label_clip = min(len(label), max(1, avail - (meta_clip + 2 if meta_clip else 0)))
            row_w = label_clip + (2 + meta_clip if meta_clip else 0)
            max_w = max(max_w, row_w)
        return min(max_w, avail)

    def _format_row(self, item: T, *, selected: bool, row_index: int = 0, panel_w: int = 0) -> str:
        label = self._display_fn(item)
        meta = self._meta_fn(item) if self._meta_fn else ""

        has_scrollbar = len(self._items) > self._height
        avail = max(1, self._cols - self._col - (1 if has_scrollbar else 0))
        meta_w = min(len(meta), min(20, avail // 4))
        label_w = min(len(label), max(1, panel_w - (meta_w + 2 if meta_w else 0)))
        label = label[:label_w]
        meta = meta[:meta_w]

        content_w = len(label) + (2 + len(meta) if meta else 0)
        pad = " " * max(0, panel_w - content_w)

        col_move = f"\033[{self._col}C" if self._col > 0 else ""
        if selected:
            inner = label + (f"  {meta}" if meta else "")
            row = f"\r{col_move}{self._BG}\033[7m{inner}{pad}\033[0m"
        else:
            inner = label + (f"  \033[2m{meta}\033[22m" if meta else "")
            row = f"\r{col_move}{self._BG}{inner}{pad}\033[0m"

        if has_scrollbar:
            sb_col = self._col + panel_w + 1  # 1-indexed terminal column
            row += f"\033[{sb_col}G{self._scrollbar_char(row_index)}"
        return row

    def _cleanup(self) -> None:
        """Restore to anchor and erase the picker area."""
        sys.stdout.write("\0338\r\033[J")
        sys.stdout.flush()

    # ── char input ──────────────────────────────────────────────────────────

    def _handle_tab_complete(self) -> bool:
        """Type the common prefix extension. Returns True (sets reopen) if chars were typed."""
        if not self._items or self._value_fn is None:
            return False
        values = [self._value_fn(item) for item in self._items]
        common = _common_prefix(values)
        effective_len = len(self._completion_prefix) + len(self._typed)
        extension = common[effective_len:]
        if not extension:
            return False
        sys.stdout.write(extension)
        sys.stdout.flush()
        self._typed += extension
        self.reopen = True
        return True

    def _handle_char(self, ch: str) -> bool:
        """Write ch at prompt caret and refresh. Returns True (sets reopen) if col changed or reopen_when fires."""
        sys.stdout.write(ch)
        sys.stdout.flush()
        self._typed += ch
        if self._refresh_fn is not None:
            new_items, new_col = self._refresh_fn(self._typed)
            if new_col != self._col or (self._reopen_when is not None and self._reopen_when(new_items)):
                self._items = new_items
                self.reopen = True
                return True
            self._items = new_items
            self._selected = 0
            self._offset = 0
        self._render()
        return False

    def _handle_backspace(self) -> bool:
        """Erase one char. Returns True when the picker should close (sets reopen or apply_backspace)."""
        if self._typed:
            # Erase last picker-typed char from the prompt line.
            sys.stdout.write("\033[D \033[D")
            sys.stdout.flush()
            self._typed = self._typed[:-1]
            if self._refresh_fn is not None:
                new_items, new_col = self._refresh_fn(self._typed)
                if new_col != self._col:
                    self._items = new_items
                    self.reopen = True
                    return True
                self._items = new_items
                self._selected = 0
                self._offset = 0
            self._render()
            return False
        else:
            # No picker-typed chars remain; erase the last buffer char visually
            # and signal the caller to apply the deletion and close.
            caret_col = self._col + self._initial_offset
            if caret_col > 0:
                sys.stdout.write("\033[D \033[D")
                sys.stdout.flush()
            self.apply_backspace = True
            return True

    # ── input ───────────────────────────────────────────────────────────────

    def _read_key(self, fd: int, sig_r: int) -> bytes:
        while True:
            if self._cancelled:
                return b"\x1b"  # triggers "cancel" in _dispatch
            r, _, _ = select.select([fd, sig_r], [], [], 1.0)
            if sig_r in r:
                os.read(sig_r, 256)  # drain wakeup bytes, loop to check _cancelled
                continue
            if not r:
                continue
            data = os.read(fd, 1)
            if data == b"\x1b":
                r2, _, _ = select.select([fd], [], [], 0.05)
                if r2:
                    data += os.read(fd, 8)
            return data

    def _dispatch(self, key: bytes) -> str:
        if key in (b"\r", b"\n"):
            return "accept"
        if key in (b"\x1b", b"\x03"):
            return "cancel"
        if key in (b"\x1b[A", b"\x1bOA", b"\x10"):   # up arrow (normal/app mode), Ctrl+P
            return "up"
        if key in (b"\x1b[B", b"\x1bOB", b"\x0e"):  # down arrow (normal/app mode), Ctrl+N
            return "down"
        if key == b"\t":
            return "tab_complete"
        if key in (b"\x7f", b"\x08"):            # Backspace, Ctrl+H
            return "backspace"
        if len(key) == 1 and 0x20 <= key[0] < 0x7F:
            return key.decode()
        return "noop"

    # ── scroll ───────────────────────────────────────────────────────────────

    def _move(self, delta: int) -> None:
        n = len(self._items)
        self._selected = max(0, min(n - 1, self._selected + delta))
        if self._selected < self._offset:
            self._offset = self._selected
        elif self._selected >= self._offset + self._height:
            self._offset = self._selected - self._height + 1


class InlineArgPrompt:
    """Single-line inline prompt for a flag argument, rendered below the cursor.

    Shows a dim label (e.g. ``--max-depth <N>:``) followed by an editable
    input area. Enter confirms; Esc / Ctrl+C cancels (returns None).
    The caller is responsible for positioning (writing \\n to move below the
    command line) before calling run() and moving the cursor back afterward.
    """

    def __init__(self, label: str, description: str = ""):
        self._label = label
        self._description = description
        self._buf = ""
        self._cancelled = False
        try:
            self._cols = os.get_terminal_size().columns
        except OSError:
            self._cols = 80

    def run(self) -> str | None:
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)

        sig_r, sig_w = os.pipe()
        os.set_blocking(sig_w, False)
        old_wakeup_fd = signal.set_wakeup_fd(sig_w, warn_on_full_buffer=False)

        result: str | None = None
        try:
            self._reserve()
            tty.setraw(fd)
            signal.signal(signal.SIGWINCH, self._on_resize)
            self._render()

            while True:
                key = self._read_key(fd, sig_r)
                if self._cancelled or key in (b"\x1b", b"\x03"):
                    break
                if key in (b"\r", b"\n"):
                    result = self._buf
                    break
                if key in (b"\x7f", b"\x08"):
                    if self._buf:
                        self._buf = self._buf[:-1]
                        self._render()
                elif len(key) == 1 and 0x20 <= key[0] < 0x7F:
                    self._buf += key.decode()
                    self._render()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            signal.set_wakeup_fd(old_wakeup_fd)
            os.close(sig_r)
            os.close(sig_w)
            self._cleanup()

        return result

    def _on_resize(self, _sig, _frame) -> None:
        self._cancelled = True

    def _reserve(self) -> None:
        """Save anchor at col 0 of the current line (caller moved us here).
        When a description is shown, an extra line is reserved below."""
        if self._description:
            sys.stdout.write("\n")   # push a second line into existence
            sys.stdout.write("\033[1A")  # come back up to the anchor line
        sys.stdout.write("\r\0337")
        sys.stdout.flush()

    def _render(self) -> None:
        """Restore to anchor, redraw description (if any) + label + typed text."""
        out = ["\0338\r\033[J"]
        if self._description:
            desc = self._description[: self._cols - 2]
            out.append(f"  \033[2m{desc}\033[0m\n")
        # \r ensures col 0 — raw-mode \n is a bare line-feed and leaves the
        # cursor at the column where the description text ended.
        out.append(f"\r\033[2m{self._label}:\033[0m {self._buf}")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _cleanup(self) -> None:
        sys.stdout.write("\0338\r\033[J")
        sys.stdout.flush()

    def _read_key(self, fd: int, sig_r: int) -> bytes:
        while True:
            if self._cancelled:
                return b"\x1b"
            r, _, _ = select.select([fd, sig_r], [], [], 1.0)
            if sig_r in r:
                os.read(sig_r, 256)
                continue
            if not r:
                continue
            data = os.read(fd, 1)
            if data == b"\x1b":
                r2, _, _ = select.select([fd], [], [], 0.05)
                if r2:
                    data += os.read(fd, 8)
            return data


class InlineMultiPicker(Generic[T]):
    """
    Multi-select inline picker rendered below the cursor.

    Space toggles the highlighted item's checked state. Enter confirms and
    returns all checked items (or just the highlighted item if nothing is
    checked). Esc / Ctrl+C cancels and returns None.

    On SIGWINCH the picker cancels, same as InlinePicker.
    """

    def __init__(
        self,
        items: list[T],
        display_fn: Callable[[T], str] = str,
        meta_fn: Callable[[T], str] | None = None,
        max_height: int = 12,
        rows_above: int = 1,
        caret_col: int = 0,
    ):
        self._items = items
        self._display_fn = display_fn
        self._meta_fn = meta_fn
        self._max_height = max_height
        self._rows_above = rows_above
        self._caret_col = caret_col

        self._selected = 0
        self._offset = 0
        self._checked: set[int] = set()
        self._cols = 80
        self._height = min(max_height, len(items))
        self._cancelled = False
        self._label_col_w = max((len(display_fn(item)) for item in items), default=4)

    def run(self) -> list[T] | None:
        """Return checked items (or [highlighted] if none), or None on cancel."""
        if not self._items:
            return None

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)

        sig_r, sig_w = os.pipe()
        os.set_blocking(sig_w, False)
        old_wakeup_fd = signal.set_wakeup_fd(sig_w, warn_on_full_buffer=False)

        result: list[T] | None = None
        try:
            self._update_size()
            self._reserve()
            tty.setraw(fd)
            signal.signal(signal.SIGWINCH, self._on_resize)
            self._render()

            while True:
                action = self._dispatch(self._read_key(fd, sig_r))
                if action == "accept":
                    if self._checked:
                        result = [self._items[i] for i in sorted(self._checked)]
                    else:
                        result = [self._items[self._selected]]
                    break
                if action == "cancel":
                    break
                if action == "up":
                    self._move(-1)
                    self._render()
                elif action == "down":
                    self._move(1)
                    self._render()
                elif action == "toggle":
                    idx = self._selected
                    if idx in self._checked:
                        self._checked.discard(idx)
                    else:
                        self._checked.add(idx)
                    self._render()
                elif action.startswith("jump:"):
                    self._jump_to(action[5:])
                    self._render()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            signal.set_wakeup_fd(old_wakeup_fd)
            os.close(sig_r)
            os.close(sig_w)
            self._cleanup()

        return result

    # ── size ────────────────────────────────────────────────────────────────

    def _update_size(self) -> None:
        sz = os.get_terminal_size()
        self._cols = sz.columns
        self._height = min(self._max_height, len(self._items), max(1, sz.lines - 3))

    def _on_resize(self, _sig, _frame) -> None:
        self._cancelled = True

    # ── drawing ─────────────────────────────────────────────────────────────

    def _reserve(self) -> None:
        sys.stdout.write("\n" * self._height)
        sys.stdout.write(_csi(f"{self._height}A") + "\r\0337")
        sys.stdout.flush()

    def _render(self) -> None:
        visible = self._items[self._offset : self._offset + self._height]
        out: list[str] = ["\0338\r\033[J"]

        for i, item in enumerate(visible):
            abs_i = self._offset + i
            out.append(self._format_row(item, checked=(abs_i in self._checked), selected=(abs_i == self._selected)))
            if i < len(visible) - 1:
                out.append("\n")

        # Re-save anchor, then move cursor back to the prompt caret.
        out.append("\0338\0337")
        if self._rows_above > 0:
            out.append(f"\033[{self._rows_above}A")
        out.append("\r")
        if self._caret_col > 0:
            out.append(f"\033[{self._caret_col}C")

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    _CHECK_ON = "[x] "
    _CHECK_OFF = "[ ] "

    def _format_row(self, item: T, *, checked: bool, selected: bool) -> str:
        label = self._display_fn(item)
        meta = self._meta_fn(item) if self._meta_fn else ""

        check = self._CHECK_ON if checked else self._CHECK_OFF
        avail = self._cols - len(check)
        # Align all labels to the widest one; give everything left to the description.
        label_col = min(self._label_col_w, avail)
        meta_w = max(0, avail - label_col - 2)
        label_padded = label[:label_col].ljust(label_col)
        meta = meta[:meta_w]

        if selected:
            inner = check + label_padded + (f"  {meta}" if meta else "")
            return f"\r\033[K\033[7m{inner}\033[K\033[0m"
        else:
            inner = check + label_padded + (f"  \033[2m{meta}\033[0m" if meta else "")
            return f"\r\033[K{inner}"

    def _cleanup(self) -> None:
        sys.stdout.write("\0338\r\033[J")
        sys.stdout.flush()

    # ── input ───────────────────────────────────────────────────────────────

    def _read_key(self, fd: int, sig_r: int) -> bytes:
        while True:
            if self._cancelled:
                return b"\x1b"
            r, _, _ = select.select([fd, sig_r], [], [], 1.0)
            if sig_r in r:
                os.read(sig_r, 256)
                continue
            if not r:
                continue
            data = os.read(fd, 1)
            if data == b"\x1b":
                r2, _, _ = select.select([fd], [], [], 0.05)
                if r2:
                    data += os.read(fd, 8)
            return data

    def _dispatch(self, key: bytes) -> str:
        if key in (b"\r", b"\n"):
            return "accept"
        if key in (b"\x1b", b"\x03"):
            return "cancel"
        if key in (b"\x1b[A", b"\x1bOA", b"\x10"):
            return "up"
        if key in (b"\x1b[B", b"\x1bOB", b"\x0e"):
            return "down"
        if key == b" ":
            return "toggle"
        if len(key) == 1 and chr(key[0]).isalnum():
            return f"jump:{chr(key[0])}"
        return "noop"

    # ── scroll / jump ────────────────────────────────────────────────────────

    def _move(self, delta: int) -> None:
        n = len(self._items)
        self._selected = max(0, min(n - 1, self._selected + delta))
        self._scroll_to_selected()

    def _jump_to(self, ch: str) -> None:
        """Move selection to the next item whose label starts with ch (case-insensitive), rotating."""
        ch_lower = ch.lower()
        candidates = [
            i for i, item in enumerate(self._items)
            if self._display_fn(item).lstrip("-")[:1].lower() == ch_lower
        ]
        if not candidates:
            return
        # Advance to the first candidate strictly after the current position; wrap on exhaustion.
        for idx in candidates:
            if idx > self._selected:
                self._selected = idx
                self._scroll_to_selected()
                return
        self._selected = candidates[0]
        self._scroll_to_selected()

    def _scroll_to_selected(self) -> None:
        if self._selected < self._offset:
            self._offset = self._selected
        elif self._selected >= self._offset + self._height:
            self._offset = self._selected - self._height + 1
