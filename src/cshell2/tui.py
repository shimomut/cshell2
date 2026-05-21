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

    Invariant: cursor is at the top-left of the reserved area both before
    and after every _render() call, and after _cleanup().
    This makes resize trivial: update size, call _render().
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

    def run(self) -> T | None:
        """
        Show the picker. Returns selected item, or None if cancelled.
        Caller must be in a context where the terminal is in a usable
        state (e.g. inside run_in_terminal()).
        """
        if not self._items:
            return None

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)
        result: T | None = None

        try:
            self._update_size()
            self._reserve()
            tty.setraw(fd)
            signal.signal(signal.SIGWINCH, self._on_resize)
            self._render()

            while True:
                action = self._dispatch(self._read_key(fd))
                if action == "accept":
                    result = self._items[self._selected]
                    break
                if action == "cancel":
                    break
                if action == "up":
                    self._move(-1)
                elif action == "down":
                    self._move(1)
                if action in ("up", "down"):
                    self._render()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            self._cleanup()

        return result

    # ── size ────────────────────────────────────────────────────────────────

    def _update_size(self) -> None:
        sz = os.get_terminal_size()
        self._cols = sz.columns
        self._height = min(self._max_height, len(self._items), max(1, sz.lines - 3))

    def _on_resize(self, _sig, _frame) -> None:
        sz = os.get_terminal_size()
        self._cols = sz.columns
        # Height stays fixed for the session to avoid reserved-area bookkeeping.
        self._render()

    # ── drawing ─────────────────────────────────────────────────────────────

    def _reserve(self) -> None:
        """Print blank lines to create space; leave cursor at top of that area."""
        sys.stdout.write("\n" * self._height)
        sys.stdout.write(_csi(f"{self._height}A") + "\r")
        sys.stdout.flush()

    def _render(self) -> None:
        """Redraw the picker. Cursor must be at the top of the reserved area
        on entry; it will be at the top of the reserved area on exit."""
        visible = self._items[self._offset : self._offset + self._height]
        out: list[str] = []

        for i, item in enumerate(visible):
            out.append(self._format_row(item, selected=(i + self._offset == self._selected)))
            if i < self._height - 1:
                out.append("\n")

        # Return cursor to top of reserved area.
        if self._height > 1:
            out.append(_csi(f"{self._height - 1}A"))
        out.append("\r")

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _format_row(self, item: T, *, selected: bool) -> str:
        label = self._display_fn(item)
        meta = self._meta_fn(item) if self._meta_fn else ""

        meta_w = min(30, self._cols // 3)
        label_w = max(1, self._cols - meta_w - 5)  # 2 prefix + 2 gap + 1 margin

        label = label[:label_w].ljust(label_w)
        meta = meta[:meta_w]

        if selected:
            row = f"\033[7m❯ {label}  \033[2m{meta}\033[0m"
        else:
            row = f"  {label}  \033[2m{meta}\033[0m"

        return f"\r\033[K{row}"

    def _cleanup(self) -> None:
        """Erase reserved lines. Cursor ends at top of the cleared area."""
        for i in range(self._height):
            sys.stdout.write("\r\033[K")
            if i < self._height - 1:
                sys.stdout.write("\n")
        if self._height > 1:
            sys.stdout.write(_csi(f"{self._height - 1}A"))
        sys.stdout.write("\r")
        sys.stdout.flush()

    # ── input ───────────────────────────────────────────────────────────────

    def _read_key(self, fd: int) -> bytes:
        data = os.read(fd, 1)
        if data == b"\x1b":
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                data += os.read(fd, 8)
        return data

    def _dispatch(self, key: bytes) -> str:
        if key in (b"\r", b"\n"):
            return "accept"
        if key in (b"\x1b", b"\x03"):
            return "cancel"
        if key in (b"\x1b[A", b"\x10"):   # up arrow, Ctrl+P
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
