"""Process multiplexing — PTY-backed subprocess slots with output buffering."""

from __future__ import annotations

import collections
import fcntl
import os
import pty
import select
import struct
import sys
import termios
import threading


class OutputBuffer:
    """Thread-safe ring buffer of raw byte chunks."""

    def __init__(self, max_chunks: int = 1000):
        self._buf: collections.deque[bytes] = collections.deque(maxlen=max_chunks)
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._buf.append(data)

    def drain(self) -> list[bytes]:
        with self._lock:
            chunks = list(self._buf)
            self._buf.clear()
            return chunks


_TRACKED_MODES = {
    b"1": "app_cursor_keys", # DECCKM — application cursor key sequences (vi, less, …)
    b"1049": "alt_screen",   # alternate screen buffer
    b"1000": "mouse_click",  # mouse click tracking
    b"1002": "mouse_btn",    # mouse button tracking
    b"1003": "mouse_all",    # all mouse motion tracking
    b"1006": "mouse_sgr",    # SGR mouse mode
}


class ProcessSlot:
    """Manages a single PTY subprocess with output buffering for context switching."""

    def __init__(self):
        self.pid: int = -1
        self.master_fd: int = -1
        self.argv: list[str] = []
        # Full history — replayed for alt-screen TUIs (idempotent paints).
        self.buffer: OutputBuffer = OutputBuffer()
        # Bytes received while backgrounded — replayed once for streaming
        # output so users don't lose lines but also don't see duplicates.
        self.missed: OutputBuffer = OutputBuffer()
        self.active: bool = False
        self.exit_code: int | None = None
        self._exit_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self.terminal_modes: dict[str, bool] = {
            name: False for name in _TRACKED_MODES.values()
        }

    def start(self, argv: list[str], env: dict[str, str], cwd: str) -> None:
        self.argv = argv
        master_fd, slave_fd = pty.openpty()

        # Set PTY size before fork so child sees correct dimensions immediately.
        # Don't set LINES/COLUMNS in env: ncurses honors those over TIOCGWINSZ
        # and then ignores SIGWINCH-driven resize (KEY_RESIZE never fires).
        rows, cols = self._get_real_terminal_size()
        if rows and cols:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        pid = os.fork()
        if pid == 0:
            # Child process
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        else:
            # Parent process
            os.close(slave_fd)
            self.pid = pid
            self.master_fd = master_fd
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()

    @staticmethod
    def _get_real_terminal_size() -> tuple[int, int]:
        """Get the real terminal size, trying multiple fds."""
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            try:
                fd = stream.fileno()
                size = os.get_terminal_size(fd)
                return size.lines, size.columns
            except (OSError, ValueError, AttributeError):
                continue
        import shutil
        size = shutil.get_terminal_size()
        return size.lines, size.columns

    def _reader_loop(self) -> None:
        try:
            while True:
                # Use select before read: on macOS, a blocking os.read() wakes
                # with EIO when the slave PTY closes, discarding buffered data.
                # select() correctly reports the fd as readable when data is
                # available even after slave close, so we get the data first.
                try:
                    r, _, _ = select.select([self.master_fd], [], [], 0.05)
                    if not r:
                        continue
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self._track_terminal_modes(data)
                self.buffer.append(data)
                if self.active:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                else:
                    self.missed.append(data)
        finally:
            try:
                _, status = os.waitpid(self.pid, 0)
                if os.WIFEXITED(status):
                    self.exit_code = os.WEXITSTATUS(status)
                else:
                    self.exit_code = -1
            except ChildProcessError:
                self.exit_code = -1
            self._exit_event.set()
            try:
                os.close(self.master_fd)
            except OSError:
                pass

    def _track_terminal_modes(self, data: bytes) -> None:
        """Scan output for DEC private mode set/reset sequences."""
        # Match \x1b[?<number>h (enable) and \x1b[?<number>l (disable)
        i = 0
        while i < len(data):
            idx = data.find(b"\x1b[?", i)
            if idx == -1:
                break
            # Parse the number after \x1b[?
            start = idx + 3
            end = start
            while end < len(data) and data[end:end + 1].isdigit():
                end += 1
            if end < len(data) and end > start:
                mode_num = data[start:end]
                action = data[end:end + 1]
                if mode_num in _TRACKED_MODES:
                    name = _TRACKED_MODES[mode_num]
                    if action == b"h":
                        self.terminal_modes[name] = True
                    elif action == b"l":
                        self.terminal_modes[name] = False
            i = end + 1 if end > idx else idx + 1

    def activate(self, *, replay_missed: bool = False) -> None:
        """Resume writing reader output to stdout.

        If *replay_missed* is True, flush bytes that arrived while inactive
        to stdout before clearing the missed-buffer. Done under the same
        buffer lock the reader takes, so nothing is dropped or duplicated
        across the activation boundary.
        """
        with self.missed._lock:
            if replay_missed:
                for chunk in self.missed._buf:
                    sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            self.missed._buf.clear()
            self.active = True

    def deactivate(self) -> None:
        self.active = False

    def suspend_terminal_modes(self) -> str:
        """Return escape sequences to undo active terminal modes on switch-away."""
        _MODE_CODES = {v: k for k, v in _TRACKED_MODES.items()}
        seq = ""
        for name, active in self.terminal_modes.items():
            if active:
                code = _MODE_CODES[name].decode()
                seq += f"\x1b[?{code}l"
        if self.terminal_modes.get("alt_screen"):
            seq += "\x1b[?25h"  # show cursor
        return seq

    def restore_terminal_modes(self) -> str:
        """Return escape sequences to re-enable terminal modes on switch-back."""
        _MODE_CODES = {v: k for k, v in _TRACKED_MODES.items()}
        seq = ""
        for name, active in self.terminal_modes.items():
            if active:
                code = _MODE_CODES[name].decode()
                seq += f"\x1b[?{code}h"
        return seq

    def replay_buffer(self) -> None:
        chunks = self.buffer.drain()
        for chunk in chunks:
            sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    def write_stdin(self, data: bytes) -> None:
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        import signal as sig

        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        # On macOS, TIOCSWINSZ on master doesn't auto-deliver SIGWINCH.
        # Send to the child's process group so all foreground processes see it.
        try:
            os.killpg(os.getpgid(self.pid), sig.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def is_alive(self) -> bool:
        return self.exit_code is None

    def wait(self, timeout: float | None = None) -> bool:
        return self._exit_event.wait(timeout)

    def kill(self) -> None:
        import signal as sig

        if self.is_alive():
            try:
                os.kill(self.pid, sig.SIGTERM)
            except ProcessLookupError:
                pass
