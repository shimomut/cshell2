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
        refresh_fn: Callable[[str], list[T]] | None = None,
        value_fn: Callable[[T], str] | None = None,
        completion_prefix: str = "",
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
        self._typed = ""
        self.reopen = False  # set True when tab-complete typed chars; caller should reopen

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
                        self.reopen = True
                        break
                elif action == "backspace":
                    self._handle_backspace()
                elif len(action) == 1:  # printable char
                    self._handle_char(action)
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
        """Create blank lines below cursor and save the top position with DECSC."""
        sys.stdout.write("\n" * self._height)
        col_move = f"\033[{self._col}C" if self._col > 0 else ""
        sys.stdout.write(_csi(f"{self._height}A") + "\r" + col_move + "\0337")
        sys.stdout.flush()

    def _render(self) -> None:
        """Restore to DECSC anchor, clear, draw rows, then place cursor at prompt caret."""
        visible = self._items[self._offset : self._offset + self._height]
        out: list[str] = ["\0338\r\033[J"]  # restore to anchor, clear to end

        for i, item in enumerate(visible):
            out.append(self._format_row(item, selected=(i + self._offset == self._selected)))
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

    def _format_row(self, item: T, *, selected: bool) -> str:
        label = self._display_fn(item)
        meta = self._meta_fn(item) if self._meta_fn else ""

        # No padding — rows stay narrow to avoid wrapping on resize.
        # The selected-row highlight extends to EOL via \033[K inside
        # reverse-video, which fills colour without adding characters.
        avail = self._cols - self._col
        meta_w = min(len(meta), min(20, avail // 4))
        label_w = max(1, avail - meta_w - 2)  # 2 gap between label and meta
        label = label[:label_w]
        meta = meta[:meta_w]

        col_move = f"\033[{self._col}C" if self._col > 0 else ""
        if selected:
            inner = label + (f"  {meta}" if meta else "")
            return f"\r{col_move}\033[K\033[7m{inner}\033[K\033[0m"
        else:
            inner = label + (f"  \033[2m{meta}\033[0m" if meta else "")
            return f"\r{col_move}\033[K{inner}"

    def _cleanup(self) -> None:
        """Restore to anchor and erase the picker area."""
        sys.stdout.write("\0338\r\033[J")
        sys.stdout.flush()

    # ── char input ──────────────────────────────────────────────────────────

    def _handle_tab_complete(self) -> bool:
        """Type the common prefix extension. Returns True if chars were typed (caller should reopen)."""
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
        return True

    def _handle_char(self, ch: str) -> None:
        """Write ch at the prompt caret and refresh the candidate list."""
        sys.stdout.write(ch)
        sys.stdout.flush()
        self._typed += ch
        if self._refresh_fn is not None:
            new_items = self._refresh_fn(self._typed)
            self._items = new_items
            self._selected = 0
            self._offset = 0
        self._render()

    def _handle_backspace(self) -> None:
        if not self._typed:
            return
        # Cursor is at (caret_row, col + len(typed)); erase the last typed char.
        sys.stdout.write("\033[D \033[D")
        sys.stdout.flush()
        self._typed = self._typed[:-1]
        if self._refresh_fn is not None:
            new_items = self._refresh_fn(self._typed)
            self._items = new_items
            self._selected = 0
            self._offset = 0
        self._render()

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
        if key in (b"\x1b[A", b"\x10"):         # up arrow, Ctrl+P
            return "up"
        if key in (b"\x1b[B", b"\x0e"):          # down arrow, Ctrl+N
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
