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
    ):
        self._items = items
        self._display_fn = display_fn
        self._meta_fn = meta_fn
        self._max_height = max_height

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
            sys.stdout.write("\033[?25l")   # hide cursor
            sys.stdout.flush()
            signal.signal(signal.SIGWINCH, self._on_resize)
            self._render()

            while True:
                action = self._dispatch(self._read_key(fd, sig_r))
                if action == "accept":
                    result = self._items[self._selected]
                    break
                if action == "cancel":
                    break
                if action in ("up", "down"):
                    self._move(-1 if action == "up" else 1)
                    self._render()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            signal.set_wakeup_fd(old_wakeup_fd)
            os.close(sig_r)
            os.close(sig_w)
            self._cleanup()
            sys.stdout.write("\033[?25h")   # restore cursor
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
        sys.stdout.write(_csi(f"{self._height}A") + "\r\0337")
        sys.stdout.flush()

    def _render(self) -> None:
        """Restore to DECSC anchor, clear, draw rows, restore anchor again."""
        visible = self._items[self._offset : self._offset + self._height]
        out: list[str] = ["\0338\r\033[J"]  # restore to anchor, clear to end

        for i, item in enumerate(visible):
            out.append(self._format_row(item, selected=(i + self._offset == self._selected)))
            if i < self._height - 1:
                out.append("\n")

        out.append("\0338\r\0337")  # restore to anchor, re-save

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _format_row(self, item: T, *, selected: bool) -> str:
        label = self._display_fn(item)
        meta = self._meta_fn(item) if self._meta_fn else ""

        # No padding — rows stay narrow to avoid wrapping on resize.
        # The selected-row highlight extends to EOL via \033[K inside
        # reverse-video, which fills colour without adding characters.
        meta_w = min(len(meta), min(20, self._cols // 4))
        label_w = max(1, self._cols - meta_w - 4)  # 2 prefix + 2 gap
        label = label[:label_w]
        meta = meta[:meta_w]

        if selected:
            inner = f"❯ {label}" + (f"  {meta}" if meta else "")
            return f"\r\033[K\033[7m{inner}\033[K\033[0m"
        else:
            inner = f"  {label}" + (f"  \033[2m{meta}\033[0m" if meta else "")
            return f"\r\033[K{inner}"

    def _cleanup(self) -> None:
        """Restore to anchor and erase the picker area."""
        sys.stdout.write("\0338\r\033[J")
        sys.stdout.flush()

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
        if key in (b"\x1b[B", b"\x0e", b"\t"):  # down arrow, Ctrl+N, Tab
            return "down"
        return "noop"

    # ── scroll ───────────────────────────────────────────────────────────────

    def _move(self, delta: int) -> None:
        n = len(self._items)
        self._selected = max(0, min(n - 1, self._selected + delta))
        if self._selected < self._offset:
            self._offset = self._selected
        elif self._selected >= self._offset + self._height:
            self._offset = self._selected - self._height + 1
