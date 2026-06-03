"""Cross-platform terminal primitives — raw mode, key reading, resize.

The rest of the shell (lineedit, tui, shell) drives the terminal through this
module instead of touching ``termios``/``tty``/``select``/``msvcrt`` directly,
so the same rendering code runs on both POSIX and native Windows.

POSIX backend: ``termios`` + ``tty`` for raw mode, ``os.read`` + ``select`` for
input, ``SIGWINCH`` for resize notification.

Windows backend: ``msvcrt`` for unbuffered key reading (translating the
``\\x00``/``\\xe0`` scan-code prefixes into the same ANSI escape sequences the
POSIX path produces), and the Win32 console API (via ``ctypes``) to enable
virtual-terminal output processing so the ANSI escapes the renderer emits are
honoured.  There is no ``SIGWINCH`` on Windows, so resize is detected by
polling :func:`os.get_terminal_size` between key reads.
"""

from __future__ import annotations

import os
import signal
import sys
import time

IS_WINDOWS = os.name == "nt"

# True when the platform delivers a signal on terminal resize.  Callers use
# this to decide between signal-driven reflow (POSIX) and poll-based resize
# detection (Windows).
HAS_SIGWINCH = hasattr(signal, "SIGWINCH")

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import select
    import termios
    import tty


# ── Windows console mode management ─────────────────────────────────────────

if IS_WINDOWS:
    _kernel32 = ctypes.windll.kernel32

    _STD_OUTPUT_HANDLE = -11
    _ENABLE_PROCESSED_OUTPUT = 0x0001
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    _DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    def _out_handle() -> int:
        return _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)

    def _get_console_mode(handle: int) -> int | None:
        mode = wintypes.DWORD()
        if _kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return mode.value
        return None

    def _set_console_mode(handle: int, mode: int) -> None:
        _kernel32.SetConsoleMode(handle, mode)

    def _enable_vt_output() -> None:
        """Turn on ANSI escape interpretation for stdout (idempotent)."""
        handle = _out_handle()
        mode = _get_console_mode(handle)
        if mode is None:
            return  # not a real console (redirected) — nothing to do
        _set_console_mode(
            handle,
            mode | _ENABLE_PROCESSED_OUTPUT | _ENABLE_VIRTUAL_TERMINAL_PROCESSING,
        )


def init() -> None:
    """One-time terminal setup. Safe to call repeatedly.

    On Windows this enables VT output processing and disables Python's
    automatic ``\\n`` → ``\\r\\n`` translation on the std streams so the
    renderer has the same byte-level control it has on POSIX (where raw mode
    turns off ONLCR).  Carriage returns are then governed entirely by console
    mode: :func:`set_raw` sets ``DISABLE_NEWLINE_AUTO_RETURN`` so a bare ``\\n``
    is a pure line-feed during rendering, and :func:`restore_mode` clears it so
    cooked output (normal ``print``) still wraps to column 0.
    """
    if not IS_WINDOWS:
        return
    _enable_vt_output()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(newline="")
        except (AttributeError, ValueError):
            pass


# ── raw mode ────────────────────────────────────────────────────────────────


def get_mode(fd: int):
    """Snapshot the current terminal mode so it can be restored later.

    Returns an opaque token (POSIX: termios attrs; Windows: None — restoration
    only needs to clear the newline flag).
    """
    if IS_WINDOWS:
        return None
    return termios.tcgetattr(fd)


def set_raw(fd: int) -> None:
    """Put the terminal into raw mode for character-at-a-time editing."""
    if IS_WINDOWS:
        _enable_vt_output()
        handle = _out_handle()
        mode = _get_console_mode(handle)
        if mode is not None:
            _set_console_mode(handle, mode | _DISABLE_NEWLINE_AUTO_RETURN)
        return
    # TCSADRAIN (not TCSAFLUSH) preserves bytes already queued in the kernel's
    # input buffer — e.g. the remainder of a pasted multi-line block.
    tty.setraw(fd, termios.TCSADRAIN)


def restore_mode(fd: int, saved) -> None:
    """Restore the terminal to the mode captured by :func:`get_mode`."""
    if IS_WINDOWS:
        handle = _out_handle()
        mode = _get_console_mode(handle)
        if mode is not None:
            _set_console_mode(handle, mode & ~_DISABLE_NEWLINE_AUTO_RETURN)
        return
    termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# ── input ───────────────────────────────────────────────────────────────────

if IS_WINDOWS:
    # Map the second byte of a \x00 / \xe0 scan-code pair to the ANSI escape
    # sequence the POSIX path would produce, so the key parsers in lineedit/tui
    # can stay platform-agnostic.
    _WIN_SCANCODES = {
        "H": b"\x1b[A",   # up
        "P": b"\x1b[B",   # down
        "K": b"\x1b[D",   # left
        "M": b"\x1b[C",   # right
        "G": b"\x1b[H",   # home
        "O": b"\x1b[F",   # end
        "R": b"\x1b[2~",  # insert
        "S": b"\x1b[3~",  # delete
        "I": b"\x1b[5~",  # page up
        "Q": b"\x1b[6~",  # page down
    }

    def _win_read_key() -> bytes:
        """Read one logical key from the console, returning ANSI-style bytes."""
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            scan = msvcrt.getwch()
            return _WIN_SCANCODES.get(scan, b"")
        # Ordinary character (control or printable). Encode to UTF-8 so the
        # byte-oriented parsers see \x1b, \r, \t, control codes, and multibyte
        # characters exactly as they would on POSIX.
        return ch.encode("utf-8")


def wait_readable(fd: int, timeout: float) -> bool:
    """Return True if a key is available within *timeout* seconds."""
    if IS_WINDOWS:
        deadline = time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
    r, _, _ = select.select([fd], [], [], timeout)
    return bool(r)


def read_key(fd: int) -> bytes:
    """Block until one logical key is available and return it as bytes.

    A "logical key" is a complete unit: a single control byte, a full UTF-8
    character (all continuation bytes included), or a complete escape sequence
    (e.g. ``b"\\x1b[A"`` for the up arrow).  This lets callers compare against
    fixed byte patterns without re-reading the stream themselves.
    """
    if IS_WINDOWS:
        while not msvcrt.kbhit():
            time.sleep(0.005)
        return _win_read_key()

    data = os.read(fd, 1)
    if not data:
        return data  # EOF
    first = data[0]
    if first == 0x1B:  # ESC — may begin an escape sequence
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            data += os.read(fd, 8)
        return data
    if first >= 0xC0:  # UTF-8 lead byte — pull in continuation bytes
        extra = 1 if first < 0xE0 else 2 if first < 0xF0 else 3
        for _ in range(extra):
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                break
            data += os.read(fd, 1)
    return data


# ── resize handling ─────────────────────────────────────────────────────────


def install_resize_handler(handler):
    """Install a SIGWINCH handler, returning the previous one (or None).

    No-op on platforms without SIGWINCH; callers fall back to polling
    :func:`os.get_terminal_size` in that case.
    """
    if not HAS_SIGWINCH:
        return None
    old = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, handler)
    return old


def restore_resize_handler(old) -> None:
    if not HAS_SIGWINCH or old is None:
        return
    signal.signal(signal.SIGWINCH, old)


def terminal_size() -> tuple[int, int]:
    """Return (columns, lines), falling back to (80, 24)."""
    try:
        sz = os.get_terminal_size()
        return sz.columns, sz.lines
    except OSError:
        return 80, 24
