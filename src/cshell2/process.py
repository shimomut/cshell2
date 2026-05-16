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


class ProcessSlot:
    """Manages a single PTY subprocess with output buffering for context switching."""

    def __init__(self):
        self.pid: int = -1
        self.master_fd: int = -1
        self.buffer: OutputBuffer = OutputBuffer()
        self.active: bool = False
        self.exit_code: int | None = None
        self._exit_event = threading.Event()
        self._reader_thread: threading.Thread | None = None

    def start(self, argv: list[str], env: dict[str, str], cwd: str) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:
            # Child process
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        else:
            # Parent process
            self.pid = pid
            self.master_fd = master_fd
            self._set_pty_size()
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()

    def _set_pty_size(self) -> None:
        try:
            rows, cols = os.get_terminal_size()
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def _reader_loop(self) -> None:
        try:
            while True:
                try:
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self.buffer.append(data)
                if self.active:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
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

    def activate(self) -> None:
        self.active = True

    def deactivate(self) -> None:
        self.active = False

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
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
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
