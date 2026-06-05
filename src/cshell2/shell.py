"""Main shell loop — input handling, command dispatch, completion integration."""

from __future__ import annotations

import ctypes
import io
import os
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import traceback
from pathlib import Path

# PTY multiplexing and raw-mode forwarding are POSIX-only.  On Windows these
# modules are absent and the code paths that use them are never reached (the
# shell runs external commands on the real console — see _execute_external).
IS_WINDOWS = os.name == "nt"
if not IS_WINDOWS:
    import fcntl
    import pty
    import termios
    import tty

from . import terminal
from .commands import arg, CommandRegistry, get_positional_completer, registry as command_registry
from .completion import (
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    Completion,
    get_argcomplete_fallback,
    get_cobra_fallback,
)
from .variables import registry as var_registry, VarCompleter
from .context import ContextManager, ContextState
from .lineedit import CONTEXT_CHANGED_SENTINEL, History, LineEditor, SWITCH_SENTINEL
from .parsing import expand_vars, split_for_completion, tokenize
from .pipeline import Redirect, Sequence, Stage, Pipeline, expand_globs, parse_line, _split_on_operators
from .process import OutputBuffer, ProcessSlot
from .prompt import get_prompt_func, set_prompt

# ---------------------------------------------------------------------------
# Thread-local stdout routing + per-slot buffering proxy
# ---------------------------------------------------------------------------

class _ThreadLocalStdout(io.TextIOBase):
    """A sys.stdout replacement that routes writes per thread.

    The main thread (no override set) writes directly to *real*.
    A Python-command thread sets an override via set_override() so its
    print() calls go to a _StdoutProxy, keeping them separate from the
    main thread's terminal output.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._local = threading.local()

    @property
    def _target(self) -> io.TextIOBase:
        return getattr(self._local, "override", None) or self._real

    def write(self, s: str) -> int:
        return self._target.write(s)

    def flush(self) -> None:
        self._target.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def buffer(self):
        return self._real.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def set_override(self, proxy) -> None:
        self._local.override = proxy

    def clear_override(self) -> None:
        self._local.override = None


class _ThreadLocalStdin(io.TextIOBase):
    """A sys.stdin replacement that routes reads per thread.

    The main thread (no override set) reads from *real*.  A pipeline
    thread sets an override pointing at a TextIOWrapper around its pipe
    fd so ``input()`` / ``sys.stdin.read()`` consume from the pipe
    instead of the terminal.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._local = threading.local()

    @property
    def _target(self) -> io.TextIOBase:
        return getattr(self._local, "override", None) or self._real

    def read(self, size: int = -1) -> str:
        return self._target.read(size)

    def readline(self, size: int = -1) -> str:
        return self._target.readline(size)

    def readlines(self, hint: int = -1) -> list:
        return self._target.readlines(hint)

    def __iter__(self):
        return iter(self._target)

    def __next__(self):
        return next(self._target)

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def buffer(self):
        target = getattr(self._local, "override", None)
        return getattr(target, "buffer", None) or self._real.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def isatty(self) -> bool:
        target = getattr(self._local, "override", None)
        if target is not None:
            return False
        return self._real.isatty()

    def set_override(self, stream) -> None:
        self._local.override = stream

    def clear_override(self) -> None:
        self._local.override = None


class _ThreadLocalStderr(io.TextIOBase):
    """A sys.stderr replacement that routes writes per thread.

    Mirrors :class:`_ThreadLocalStdout` for stderr so a pipeline thread
    can redirect its diagnostic output independently of the main
    thread.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._local = threading.local()

    @property
    def _target(self) -> io.TextIOBase:
        return getattr(self._local, "override", None) or self._real

    def write(self, s: str) -> int:
        return self._target.write(s)

    def flush(self) -> None:
        self._target.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def buffer(self):
        return self._real.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def set_override(self, stream) -> None:
        self._local.override = stream

    def clear_override(self) -> None:
        self._local.override = None


class _StdoutProxy(io.TextIOBase):
    """Per-command buffering proxy.

    Starts inactive (buffering).  Call activate(raw_mode=True) once the
    terminal is in raw mode: it replays the buffer (converting \\n → \\r\\n)
    and then forwards subsequent writes directly.  deactivate() resumes
    buffering (used when the user switches away).
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._buf = io.StringIO()
        self._active = False
        self._raw_mode = False
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def write(self, s: str) -> int:
        with self._lock:
            if self._active:
                if self._raw_mode:
                    s = s.replace("\n", "\r\n")
                return self._real.write(s)
            return self._buf.write(s)

    def flush(self) -> None:
        with self._lock:
            if self._active:
                self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    def activate(self, raw_mode: bool = False) -> None:
        """Replay buffer to real stdout and start writing live."""
        with self._lock:
            self._raw_mode = raw_mode
            content = self._buf.getvalue()
            if content:
                if raw_mode:
                    content = content.replace("\n", "\r\n")
                self._real.write(content)
                self._real.flush()
                self._buf = io.StringIO()
            self._active = True

    def deactivate(self) -> None:
        with self._lock:
            self._active = False
            self._raw_mode = False

    def replay(self) -> None:
        """Drain buffer to real stdout (called on switch-back, cooked mode)."""
        with self._lock:
            content = self._buf.getvalue()
            if content:
                self._real.write(content)
                self._real.flush()
                self._buf = io.StringIO()


class _NullBuffer:
    """Stub matching OutputBuffer.drain() used in the run() loop."""
    def drain(self) -> list:
        return []


class _PyStageHandle:
    """Lightweight handle for a Python-command stage running in a thread.

    Mirrors the attributes the pipeline driver in _execute_pipeline uses
    on subprocess.Popen (`wait()`, `returncode`-style exit code) so the
    two worker types can share one wait loop.
    """

    __slots__ = ("cmd_name", "thread", "done", "exit_code", "_io_objs", "interrupted")

    def __init__(self, cmd_name: str) -> None:
        self.cmd_name = cmd_name
        self.thread: threading.Thread | None = None
        self.done = threading.Event()
        self.exit_code: int | None = None
        # File objects whose underlying fds the worker thread reads/writes.
        # On interrupt() we close them so any blocked I/O raises and the
        # thread can unwind.
        self._io_objs: list = []
        # Set by interrupt() so the worker can tell "the parent killed me"
        # apart from a genuine error.  Any I/O exception that arrives after
        # this flag is set is expected (closed wrappers) and is silenced.
        self.interrupted: bool = False

    def wait(self) -> int:
        if self.thread is not None:
            # Poll so KeyboardInterrupt in the main thread can break out.
            while not self.done.wait(timeout=0.1):
                pass
        return self.exit_code or 0

    def interrupt(self) -> None:
        """Best-effort interruption of the worker thread.

        Python threads can't be cancelled, so we close the file objects
        the worker is reading from / writing to.  Any in-flight read or
        write raises, the worker's exception handler converts it to a
        normal exit, and the wait below returns promptly.

        A pure-CPU loop inside a Python command is still uninterruptible
        — flagged in doc/limitations.md.
        """
        self.interrupted = True
        for obj in self._io_objs:
            try:
                obj.close()
            except Exception:
                pass


_current_slot = threading.local()

# Set on threads spawned by _execute_pipeline for Python-command stages.
# passthrough_run / passthrough_input check this and refuse, since stdin
# and stdout are wired to pipe ends, not the terminal.
_in_pipeline = threading.local()


def passthrough_run(argv: list[str], **popen_kwargs) -> int:
    """Run an interactive subprocess from inside a Python command thread.

    A Python @registry.command runs in a background thread while the main
    thread holds stdin in raw mode and forwards bytes to the slot.  Calling
    ``subprocess.run`` directly inside such a command makes the child
    inherit the real stdin — which the main thread is also reading — and
    keystrokes are split unpredictably between the two.

    ``passthrough_run`` runs the subprocess against a PTY owned by the
    enclosing :class:`PythonCommandSlot` so the main thread keeps a single
    consistent view of stdin: it forwards bytes to the slot's PTY master,
    intercepts ``Ctrl+]`` for context switching, and the subprocess sees a
    full TTY on its fd 0/1/2.

    Outside a Python command thread (e.g. from a synchronous handler) the
    function falls back to ``subprocess.run(argv, **popen_kwargs)``.

    Returns the subprocess's exit code.
    """
    if getattr(_in_pipeline, "flag", False):
        raise RuntimeError(
            "passthrough_run cannot be used inside a piped Python command "
            "(stdin/stdout are wired to pipes, not the terminal)"
        )
    slot = getattr(_current_slot, "slot", None)
    if slot is None:
        return subprocess.run(argv, **popen_kwargs).returncode

    return slot._run_in_pty(argv, popen_kwargs)


def passthrough_input(prompt: str = "") -> str:
    """Read a line from real stdin from inside a Python command thread.

    Built-in ``input()`` would race the main forwarding thread for stdin
    bytes, and even when it won, raw mode would suppress echo and turn
    Enter into ``\\r``.  ``passthrough_input`` coordinates with the main
    forwarding loop: it asks the loop to surrender stdin and restore
    cooked terminal mode, calls :func:`input` on the slot thread, then
    hands control back.

    Outside a Python command thread, falls back to plain ``input(prompt)``.
    """
    if getattr(_in_pipeline, "flag", False):
        raise RuntimeError(
            "passthrough_input cannot be used inside a piped Python command "
            "(stdin/stdout are wired to pipes, not the terminal)"
        )
    slot = getattr(_current_slot, "slot", None)
    if slot is None:
        return input(prompt)
    return slot._run_input(prompt)


class PythonCommandSlot:
    """Manages a Python @registry.command running in a background thread.

    Implements the same runtime interface as ProcessSlot so the shell's
    run() loop and context machinery can treat both uniformly.
    """

    def __init__(self, cmd, raw_args: list[str]) -> None:
        self._cmd = cmd
        self._raw_args = raw_args
        self.argv: list[str] = [cmd.name] + raw_args
        self._thread: threading.Thread | None = None
        self._proxy: _StdoutProxy | None = None
        self._exit_exception: BaseException | None = None
        self._finished = threading.Event()
        # Stub attributes expected by the run() loop
        self.buffer = _NullBuffer()
        self.exit_code: int | None = None
        # PTY state — created on demand by passthrough_run().  When a
        # subprocess is running here, the main thread reads stdin and writes
        # to master_fd; a reader thread copies master_fd output to stdout.
        self._pty_master_fd: int = -1
        self._pty_subproc: subprocess.Popen | None = None
        self._pty_reader: threading.Thread | None = None
        self._pty_buffer = OutputBuffer()
        self._pty_active = False
        self._pty_lock = threading.Lock()
        # passthrough_input() coordination — events are flipped by the
        # slot thread; the main forwarding loop watches _input_request.
        self._input_request = threading.Event()
        self._input_released = threading.Event()
        self._input_resume = threading.Event()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Spawn the command thread.  stdout starts buffered (inactive)."""
        real = getattr(sys.stdout, "_real", sys.stdout)
        self._proxy = _StdoutProxy(real)
        self._thread = threading.Thread(
            target=self._run,
            name=f"pycmd-{self._cmd.name}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        if hasattr(sys.stdout, "set_override"):
            sys.stdout.set_override(self._proxy)
        _current_slot.slot = self
        try:
            self._cmd.invoke(self._raw_args)
        except SystemExit as e:
            self._exit_exception = e
        except KeyboardInterrupt as e:
            self._exit_exception = e
        except Exception as e:
            self._exit_exception = e
        finally:
            _current_slot.slot = None
            if hasattr(sys.stdout, "clear_override"):
                sys.stdout.clear_override()
            self.exit_code = self._compute_exit_code()
            self._finished.set()

    def _compute_exit_code(self) -> int:
        exc = self._exit_exception
        if exc is None:
            return 0
        if isinstance(exc, SystemExit):
            code = exc.code
            return code if isinstance(code, int) else (1 if code else 0)
        if isinstance(exc, KeyboardInterrupt):
            return 130
        return 1

    # --- ProcessSlot-compatible interface ------------------------------------

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def activate(self, raw_mode: bool = False) -> None:
        if self._proxy:
            self._proxy.activate(raw_mode=raw_mode)
        # Drain any PTY output that arrived while inactive, then resume
        # live forwarding from the reader thread.
        with self._pty_lock:
            if self._pty_master_fd >= 0:
                chunks = self._pty_buffer.drain()
                for chunk in chunks:
                    try:
                        sys.stdout.buffer.write(chunk)
                    except OSError:
                        pass
                try:
                    sys.stdout.buffer.flush()
                except OSError:
                    pass
                self._pty_active = True

    def deactivate(self) -> None:
        if self._proxy:
            self._proxy.deactivate()
        with self._pty_lock:
            if self._pty_master_fd >= 0:
                self._pty_active = False

    def replay_buffer(self) -> None:
        if self._proxy:
            self._proxy.replay()
        with self._pty_lock:
            if self._pty_master_fd >= 0:
                chunks = self._pty_buffer.drain()
                for chunk in chunks:
                    try:
                        sys.stdout.buffer.write(chunk)
                    except OSError:
                        pass
                try:
                    sys.stdout.buffer.flush()
                except OSError:
                    pass

    def kill(self) -> None:
        """Inject KeyboardInterrupt into the command thread."""
        if self._thread and self._thread.is_alive():
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(self._thread.ident),
                ctypes.py_object(KeyboardInterrupt),
            )

    def restore_terminal_modes(self) -> str:
        return ""

    def suspend_terminal_modes(self) -> str:
        return ""

    def write_stdin(self, data: bytes) -> None:
        """Forward bytes to a passthrough_run() subprocess, if one is active."""
        with self._pty_lock:
            fd = self._pty_master_fd
            if fd < 0:
                return
        try:
            os.write(fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        """Propagate a SIGWINCH-driven resize to a passthrough_run() subprocess."""
        with self._pty_lock:
            fd = self._pty_master_fd
            pid = self._pty_subproc.pid if self._pty_subproc else -1
        if fd < 0:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        if pid > 0:
            try:
                os.killpg(os.getpgid(pid), signal.SIGWINCH)
            except (OSError, ProcessLookupError):
                pass

    # --- passthrough_run() implementation -----------------------------------

    def _run_in_pty(self, argv: list[str], popen_kwargs: dict) -> int:
        """Run *argv* on a slot-owned PTY; main thread forwards stdin via write_stdin."""
        # Pause the buffering stdout proxy: while the subprocess runs, its
        # output is written to the slot's PTY master and copied to stdout
        # by a reader thread.
        self._proxy.deactivate()
        master_fd, slave_fd = pty.openpty()
        try:
            rows, cols = ProcessSlot._get_real_terminal_size()
            if rows and cols:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

        env = popen_kwargs.pop("env", None) or dict(os.environ)
        # Tell the child the terminal it sees is a TTY.
        env.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))

        try:
            proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                start_new_session=True,
                **popen_kwargs,
            )
        finally:
            os.close(slave_fd)

        with self._pty_lock:
            self._pty_master_fd = master_fd
            self._pty_subproc = proc
            self._pty_active = True

        reader = threading.Thread(
            target=self._pty_reader_loop,
            args=(master_fd,),
            name=f"pycmd-pty-{self._cmd.name}",
            daemon=True,
        )
        with self._pty_lock:
            self._pty_reader = reader
        reader.start()

        try:
            proc.wait()
        finally:
            # Wait for reader to drain any remaining output, then tear down.
            reader.join(timeout=1.0)
            with self._pty_lock:
                self._pty_active = False
                self._pty_master_fd = -1
                self._pty_subproc = None
                self._pty_reader = None
            try:
                os.close(master_fd)
            except OSError:
                pass
            # Re-enable the buffering proxy in raw mode for any remaining
            # prints from the Python command after the subprocess returns.
            self._proxy.activate(raw_mode=True)

        return proc.returncode if proc.returncode is not None else 1

    def _pty_reader_loop(self, master_fd: int) -> None:
        while True:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.05)
                if not r:
                    if self._pty_subproc and self._pty_subproc.poll() is not None:
                        # Drain any final bytes before returning.
                        try:
                            r2, _, _ = select.select([master_fd], [], [], 0)
                            if not r2:
                                break
                        except OSError:
                            break
                    continue
                data = os.read(master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            self._pty_buffer.append(data)
            if self._pty_active:
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except OSError:
                    pass

    # --- passthrough_input() implementation ---------------------------------

    def _run_input(self, prompt: str) -> str:
        """Read a line via input() while the main loop yields stdin and cooked mode."""
        # Drain pending output so the prompt isn't preceded by buffered text.
        self._proxy.deactivate()
        self._proxy.replay()
        self._input_resume.clear()
        self._input_released.clear()
        self._input_request.set()
        # Wait for the main loop to release stdin and restore cooked mode.
        self._input_released.wait()
        try:
            return input(prompt)
        finally:
            self._input_request.clear()
            self._input_resume.set()
            self._proxy.activate(raw_mode=True)


_DEFAULT_CONFIG_PATH = Path(__file__).parent / "_config.py"


def _is_continuation(line: str) -> bool:
    """Return True if *line* ends with an unescaped backslash (line continuation).

    An even number of trailing backslashes means the last one is escaped (e.g.
    ``echo \\\\`` has two backslashes, none of which continue the line).
    Trailing spaces/tabs after the backslash are ignored so that accidental
    trailing whitespace does not prevent continuation from being recognised.
    """
    s = line.rstrip(" \t")
    count = 0
    for ch in reversed(s):
        if ch == "\\":
            count += 1
        else:
            break
    return count % 2 == 1


def _strip_continuation(line: str) -> str:
    """Remove the trailing continuation backslash (and any trailing whitespace before it).

    The result is ready to be concatenated with the next continuation line.
    Leading whitespace in the next line is preserved so indented continuations
    (the common style) work naturally::

        docker run --rm \\
          -v /foo:/bar      →  joined as  "docker run --rm   -v /foo:/bar"
    """
    return line.rstrip(" \t")[:-1]


def _positional_index(args: list[str], options_completer) -> int:
    """Return the number of positional (non-flag) arguments in *args*.

    Flags are skipped without counting: boolean flags advance by 1 token;
    value-taking flags (those listed in ``options_completer.args``) advance
    by 2 tokens because they consume the following token as their value.
    """
    pos = 0
    i = 0
    value_taking = (
        set(options_completer.args)
        if options_completer and hasattr(options_completer, "args")
        else set()
    )
    while i < len(args):
        token = args[i]
        if token.startswith(("-", "+")):
            i += 2 if token in value_taking else 1
        else:
            pos += 1
            i += 1
    return pos


def _flag_label(flag: str, arg_hint: str, description: str) -> str:
    """Consistent status-bar label for a flag, used across all call sites."""
    if arg_hint and description:
        return f"{flag} <{arg_hint}>: {description}"
    if description:
        return f"{flag}: {description}"
    if arg_hint:
        return f"{flag} <{arg_hint}>"
    return flag


def _label_from_arg(param) -> str:
    """Format an Arg descriptor as a status-bar label."""
    name = param.names[0]
    help_text = param.kwargs.get("help", "")
    if help_text:
        return f"{name}: {help_text}"
    return name


def _positional_label(cmd, pos_idx: int, command_name: str, args: list[str]) -> str:
    """Return a status-bar label for the positional argument at *pos_idx*.

    First consults the slot's completer via ``describe_slot(args, pos_idx)``
    so completers whose role depends on preceding args (e.g. tar's first
    positional, which flips between archive and member when ``-f`` is used)
    can override the static label.  Falls back to the ``help=`` text on the
    matching ``arg()`` descriptor — wildcard positionals reuse their label
    for every slot from their declared position onward.
    """
    if cmd is None or cmd.params is None:
        return f"arg {pos_idx + 1}"
    completer = get_positional_completer(cmd.completers, pos_idx)
    if completer is not None:
        dynamic = completer.describe_slot(args, pos_idx)
        if dynamic is not None:
            return dynamic
    from .commands import _is_flag_name
    positionals = [a for a in cmd.params if a.names and not _is_flag_name(a.names[0])]
    if not positionals:
        return f"arg {pos_idx + 1}"
    if pos_idx < len(positionals):
        return _label_from_arg(positionals[pos_idx])
    # Beyond the declared list: if the last positional is a wildcard, reuse
    # its label for every subsequent slot.
    last = positionals[-1]
    if last.kwargs.get("nargs") in ("*", "+"):
        return _label_from_arg(last)
    return f"arg {pos_idx + 1}"


class Shell:
    def __init__(self):
        # Enable VT output / disable newline translation (Windows) before any
        # rendering or stdout wrapping happens.
        terminal.init()
        os.environ.setdefault("PWD", os.getcwd())
        self.registry = command_registry
        self.context_manager = ContextManager()
        self.context_manager.create("default")
        self._register_builtins()
        self.registry.mark_builtins()
        var_registry.mark_builtins()
        self._load_user_config()
        # Install thread-local stdio routers so Python command threads can
        # rebind their own stdin/stdout/stderr (for buffering proxies or pipe
        # ends) without disturbing the main thread.
        if not isinstance(sys.stdout, _ThreadLocalStdout):
            sys.stdout = _ThreadLocalStdout(sys.stdout)
        if not isinstance(sys.stdin, _ThreadLocalStdin):
            sys.stdin = _ThreadLocalStdin(sys.stdin)
        if not isinstance(sys.stderr, _ThreadLocalStderr):
            sys.stderr = _ThreadLocalStderr(sys.stderr)

        history_path = Path.home() / ".cshell2" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)

        history = History(history_path)
        self._line_editor = LineEditor(
            history=history,
            get_completions=self._get_completions,
            get_prompt=lambda: get_prompt_func()(self.context_manager),
            switch_fn=self._handle_switch,
            get_arg_info=self._get_arg_info,
        )

        self._command_completer = CommandNameCompleter(self.registry)
        self._file_completer = FileCompleter()
        self._var_completer = VarCompleter()

    def _get_completions(self, line_before_cursor: str) -> tuple[list[Completion], str, str]:
        # Isolate the current pipeline stage so completions for `ls | grep -`
        # are computed against `grep`, not `ls`.
        stage_line = _split_on_operators(line_before_cursor, [";", "&&", "||", "|"])[-1][1]
        tokens, prefix = split_for_completion(stage_line)

        # Expand the first token if it is an alias, so completions for
        # `hp <TAB>` come from the expansion's resolved command.
        if tokens:
            expansion = self.registry.get_alias(tokens[0])
            if expansion is not None:
                expansion_tokens = tokenize(expansion)
                if expansion_tokens:
                    tokens = expansion_tokens + tokens[1:]

        if not tokens:
            # Bare KEY=VALUE assignment (e.g. "aws_region=us-<TAB>"): delegate
            # to VarCompleter for value-side completion.
            if "=" in prefix:
                ctx = CompletionContext(
                    command=None,
                    args=[],
                    arg_index=0,
                    prefix=prefix,
                    line=line_before_cursor,
                    shell_context=self.context_manager.current(),
                )
                return self._var_completer.complete(ctx), prefix, "variable"

            ctx = CompletionContext(
                command=None,
                args=[],
                arg_index=0,
                prefix=prefix,
                line=line_before_cursor,
                shell_context=self.context_manager.current(),
            )
            return self._command_completer.complete(ctx), prefix, "command"

        command_name = tokens[0]
        args = tokens[1:]
        arg_index = len(args)

        ctx = CompletionContext(
            command=command_name,
            args=args,
            arg_index=arg_index,
            prefix=prefix,
            line=line_before_cursor,
            shell_context=self.context_manager.current(),
        )

        cmd = self.registry.get(command_name)

        # Tree-shaped command: resolve to the current node, then offer
        # sub-command names / positionals / inherited flags.
        if cmd is not None and cmd.children:
            node, remaining_args = cmd.resolve(args)
            # Update ctx so positional completers see the correct index
            # (the position relative to the resolved node).
            tree_ctx = CompletionContext(
                command=command_name,
                args=remaining_args,
                arg_index=len(remaining_args),
                prefix=prefix,
                line=line_before_cursor,
                shell_context=self.context_manager.current(),
            )
            return self._complete_tree_node(node, tree_ctx, prefix)

        completers_dict = cmd.completers if cmd else None

        has_completer = False
        completions: list[Completion] = []
        label = command_name  # default: just show the command name

        if completers_dict:
            options_completer = completers_dict.get(None)
            pos_idx = _positional_index(args, options_completer)
            positional_completer = get_positional_completer(completers_dict, pos_idx)

            # When the last arg is a value-taking flag (e.g. "du -d <TAB>"),
            # suppress positional/file completion and return a hint instead.
            # Skip when the user is already typing another flag (prefix starts
            # with "-" or "+") — they should see the options picker, not the hint.
            if (options_completer and ctx.args and not ctx.prefix.startswith(("-", "+"))
                    and hasattr(options_completer, "get_preceding_flag_hint")):
                hint_info = options_completer.get_preceding_flag_hint(ctx)
                if hint_info:
                    flag, arg_hint, description, value_completer = hint_info
                    flag_label = _flag_label(flag, arg_hint, description)
                    if value_completer:
                        # Flag has a dedicated value completer (e.g. -C DIR → DirCompleter).
                        return value_completer.complete(ctx), ctx.prefix, flag_label
                    # No value completer: suppress file fallback and show an inline hint.
                    return [Completion(
                        value=flag,
                        display=f"<{arg_hint}>",
                        description=description,
                        arg_hint=arg_hint,
                        is_arg_hint=True,
                    )], ctx.prefix, flag_label

            # Options completer takes priority when typing a flag-prefixed token.
            if options_completer and ctx.prefix.startswith(("-", "+")):
                has_completer = True
                if options_completer.should_activate(ctx):
                    completions = options_completer.complete(ctx)
                    label = f"{command_name} option"

            # Positional completer as fallback (or primary when no "-" prefix).
            if not completions and positional_completer:
                has_completer = True
                if positional_completer.should_activate(ctx):
                    completions = positional_completer.complete(ctx)
                    label = _positional_label(cmd, pos_idx, command_name, args)

        # Cobra-protocol fallback: many modern Go CLIs (kubectl, helm, gh,
        # argocd, …) expose a hidden ``__complete`` subcommand.  Try it when
        # no registered completer produced candidates, before file fallback.
        if not completions:
            cobra = get_cobra_fallback()
            if cobra is not None and cobra.should_activate(ctx):
                completions = cobra.complete(ctx)

        # argcomplete fallback: the de-facto Python CLI completion library
        # (pipx, conda, pre-commit, tox, pdm, httpie, …).  Detection is done
        # by inspecting the script for the ``PYTHON_ARGCOMPLETE_OK`` marker,
        # so it never invokes side-effecting tools blindly.
        if not completions:
            argc = get_argcomplete_fallback()
            if argc is not None and argc.should_activate(ctx):
                completions = argc.complete(ctx)

        if not completions and not has_completer:
            completions = self._file_completer.complete(ctx)

        return completions, prefix, label

    def _get_arg_info(self, buf: str, cursor: int) -> str | None:
        """Return a status-bar description for the token the caret sits on.

        Handles three cases:
        - Flag token (``--flag``): returns ``"--flag: description"``
        - Flag value (token immediately after a value-taking flag): returns
          the flag's own description so the context stays visible
        - Positional arg: returns the param name (and help text when available)
        """
        # Extract the word surrounding cursor (scan left and right past non-space).
        start, end = cursor, cursor
        while start > 0 and buf[start - 1] not in (" ", "\t"):
            start -= 1
        while end < len(buf) and buf[end] not in (" ", "\t"):
            end += 1
        token = buf[start:end]

        # Parse everything before the token to get the command/args context.
        pre = buf[:start].rstrip()
        stage_pre = _split_on_operators(pre, [";", "&&", "||", "|"])[-1][1]
        tokens_before, _ = split_for_completion(stage_pre + " ")

        if not tokens_before:
            if not token:
                return None
            # Caret is on the command name itself — show its help text.
            cmd = self.registry.get(token)
            if cmd is not None and cmd.description:
                return f"{token}: {cmd.description}"
            return None

        command_name = tokens_before[0]
        preceding_args = tokens_before[1:]

        cmd = self.registry.get(command_name)
        if cmd is None:
            return None

        # Resolve to the right node and options_completer.
        if cmd.children:
            node, remaining_args = cmd.resolve(preceding_args)
            options_completer = node.merged_options_completer()
        else:
            node = cmd
            remaining_args = preceding_args
            options_completer = cmd.completers.get(None)

        if token.startswith(("-", "+")):
            # ── Flag token ────────────────────────────────────────────────────
            if options_completer is None or not hasattr(options_completer, "options"):
                return None
            desc = options_completer.options.get(token, "")
            arg_hint = getattr(options_completer, "args", {}).get(token, "")
            result = _flag_label(token, arg_hint, desc)
            return result if result != token else None

        # ── Non-flag token: positional arg or value for a preceding flag ───────

        value_taking = set(getattr(options_completer, "args", {})) if options_completer else set()
        if remaining_args and remaining_args[-1].startswith(("-", "+")) and remaining_args[-1] in value_taking:
            # Caret is on a flag value — show the flag's description.
            flag = remaining_args[-1]
            desc = getattr(options_completer, "options", {}).get(flag, "")
            arg_hint = getattr(options_completer, "args", {}).get(flag, "")
            result = _flag_label(flag, arg_hint, desc)
            return result if result != flag else None

        # Caret is on a positional arg.
        pos_idx = _positional_index(remaining_args, options_completer)

        # For tree commands: if this slot is a sub-command name, show its
        # description (or fall back to "<cmd> subcommand" for a partial token
        # that doesn't yet match a child — mirrors _complete_tree_node).
        if node.children and pos_idx == 0:
            if token in node.children:
                child = node.children[token]
                if child.description:
                    return f"{command_name} {token}: {child.description}"
                return f"{command_name} {token}"
            return f"{command_name} subcommand"

        return _positional_label(node, pos_idx, command_name, remaining_args)

    def _complete_tree_node(self, node, ctx, prefix):
        """Compute completions when the user is typing within a sub-command tree.

        *node* is the resolved node (the deepest sub-command in ctx.args).
        *ctx.args* are the tokens remaining after stripping consumed
        sub-command names; *ctx.arg_index* reflects that.
        """
        cmd_name = ctx.command or ""
        # Build a merged options completer (this node + ancestors).
        merged_options = node.merged_options_completer()

        # Preceding-flag hint: if last completed token is a value-taking flag
        # known at this node or an ancestor, show its value completer / hint.
        if (merged_options and ctx.args and not ctx.prefix.startswith("-")
                and hasattr(merged_options, "get_preceding_flag_hint")):
            hint_info = merged_options.get_preceding_flag_hint(ctx)
            if hint_info:
                flag, arg_hint, description, value_completer = hint_info
                flag_label = f"{flag}: {description}" if description else f"{flag} <{arg_hint}>"
                if value_completer:
                    return value_completer.complete(ctx), ctx.prefix, flag_label
                return [Completion(
                    value=flag,
                    display=f"<{arg_hint}>",
                    description=description,
                    arg_hint=arg_hint,
                    is_arg_hint=True,
                )], ctx.prefix, flag_label

        # Typing a flag → offer all flags from this node + ancestors.
        if ctx.prefix.startswith("-"):
            if merged_options and merged_options.should_activate(ctx):
                return merged_options.complete(ctx), prefix, f"{cmd_name} option"
            return [], prefix, f"{cmd_name} option"

        # Compute positional index relative to this node, ignoring flag tokens
        # and their values.
        pos_idx = _positional_index(ctx.args, merged_options)

        # If this is an interior group, the next positional is a sub-command
        # name.  Offer the children at the leftmost positional slot only —
        # extra positionals after a missing match should fall back to file
        # completion (or this node's own positional completer, if any).
        if node.children and pos_idx == 0:
            results = []
            for name in sorted(node.children):
                child = node.children[name]
                if name.startswith(ctx.prefix):
                    results.append(Completion(value=name, description=child.description))
            return results, prefix, f"{cmd_name} subcommand"

        # Leaf-or-deeper: use the resolved node's own positional completers.
        positional_completer = get_positional_completer(node.completers, pos_idx)
        if positional_completer is not None and positional_completer.should_activate(ctx):
            return positional_completer.complete(ctx), prefix, _positional_label(node, pos_idx, cmd_name, ctx.args)

        # If the node has no positional completer registered for this slot,
        # fall back to file completion only when the node has no positional
        # completers at all (i.e. it didn't declare any positionals).  This
        # matches the flat-command behaviour where a registered completer
        # returning [] suppresses file fallback.
        if not any(k is not None for k in node.completers):
            return self._file_completer.complete(ctx), prefix, cmd_name
        return [], prefix, cmd_name

    def _register_builtins(self) -> None:
        from .completion import (
            CallbackCompleter, ChoiceCompleter, Completer, Completion, DirCompleter,
        )

        @self.registry.command(
            name="cd",
            help="Change directory.",
            params=[arg("path", nargs="?", default="~", completer=DirCompleter())],
        )
        def cd(path):
            target = os.path.expanduser(path)
            try:
                os.chdir(target)
                os.environ["PWD"] = os.getcwd()
            except OSError as e:
                print(f"cd: {e}")

        @self.registry.command(name="exit", help="Exit the shell.")
        def exit_shell():
            running = self._running_contexts()
            if running and not self._confirm_exit(running):
                return
            raise SystemExit(0)

        @self.registry.command(name="reload", help="Reload ~/.cshell2/config.py.")
        def reload_config():
            self.registry.clear_user_commands()
            var_registry.clear_user_vars()
            set_prompt(None)
            self._load_user_config()
            print("Config reloaded.")

        @self.registry.command(
            name="var",
            help=(
                "Set, unset, or list context variables.\n\n"
                "  var              list all registered vars and env vars\n"
                "  var NAME         print current value of NAME\n"
                "  var NAME=VALUE   set NAME to VALUE\n"
                "  var NAME=        unset NAME (remove from env)\n\n"
                "NAME may be a registered Python-backed variable (e.g. 'aws_region')\n"
                "or a plain environment variable.  Registered variables handle their\n"
                "own set logic (e.g. writing multiple env keys at once)."
            ),
            params=[arg("assignments", nargs="*", metavar="NAME[=VALUE]",
                        completer=VarCompleter())],
        )
        def var_cmd(assignments):
            if not assignments:
                # List registered Python-backed vars first, then plain env.
                py_vars = var_registry.all()
                if py_vars:
                    print("[vars]")
                    for v in py_vars:
                        val = v.get()
                        val_str = val if val is not None else "(unset)"
                        desc = f"  # {v.description}" if v.description else ""
                        print(f"  {v.name}={val_str}{desc}")
                    print("[env]")
                for key, value in sorted(os.environ.items()):
                    print(f"  {key}={value}")
                return
            for assignment in assignments:
                if "=" in assignment:
                    key, _, value = assignment.partition("=")
                    if value == "":
                        self._unset_variable(key)
                    else:
                        self._set_variable(key, value)
                elif var_registry.get(assignment) is not None:
                    # 'var NAME' with no '=' → print current value of Python-backed var
                    v = var_registry.get(assignment)
                    val = v.get()
                    print(f"{assignment}={val}" if val is not None else f"{assignment}=(unset)")
                elif assignment in os.environ:
                    # 'var NAME' for a plain env var → print its value
                    print(f"{assignment}={os.environ[assignment]}")
                else:
                    print(f"var: invalid argument '{assignment}' (expected NAME=VALUE or NAME= to unset)")

        @self.registry.command(
            name="alias",
            help=(
                "Define or list command aliases.\n\n"
                "  alias                  list all aliases\n"
                "  alias NAME             show the expansion of NAME\n"
                "  alias NAME=EXPANSION   define NAME as a shorthand for EXPANSION\n\n"
                "Aliases expand the first token of a command line.  Quote the\n"
                "expansion if it contains spaces:  alias hp='awsut hyperpod'."
            ),
            params=[arg("assignments", nargs="*", metavar="NAME[=EXPANSION]")],
        )
        def alias_cmd(assignments):
            if not assignments:
                aliases = self.registry.list_aliases()
                if not aliases:
                    return
                for name in sorted(aliases):
                    print(f"alias {name}={aliases[name]!r}")
                return
            for assignment in assignments:
                if "=" in assignment:
                    name, _, expansion = assignment.partition("=")
                    if not name:
                        print(f"alias: invalid name in '{assignment}'")
                        continue
                    self.registry.alias(name, expansion)
                else:
                    expansion = self.registry.get_alias(assignment)
                    if expansion is None:
                        print(f"alias: {assignment}: not found")
                    else:
                        print(f"alias {assignment}={expansion!r}")

        @self.registry.command(
            name="unalias",
            help="Remove one or more aliases.",
            params=[arg("names", nargs="+", metavar="NAME",
                        completer=CallbackCompleter(
                            lambda: sorted(self.registry.list_aliases())))],
        )
        def unalias_cmd(names):
            for name in names:
                if not self.registry.unalias(name):
                    print(f"unalias: {name}: not found")

        @self.registry.command(
            name="help",
            help="Show help for a command, or list all commands.",
            params=[arg("command_name", nargs="?", default="",
                        completer=CallbackCompleter(lambda: sorted(self.registry.list_commands())))],
        )
        def help_cmd(command_name: str = ""):
            if command_name:
                cmd = self.registry.get(command_name)
                if cmd:
                    print(f"{cmd.name}: {cmd.help_text or 'No help available.'}")
                else:
                    print(f"Unknown command: {command_name}")
            else:
                print("Available commands:")
                for name in sorted(self.registry.list_commands()):
                    cmd = self.registry.get(name)
                    desc = cmd.help_text.split("\n")[0] if cmd.help_text else ""
                    print(f"  {name:20s} {desc}")

        _names_after_subcommands = {"switch", "kill"}

        class ContextNameCompleter(Completer):
            def __init__(self, cm):
                self._cm = cm

            def should_activate(self, ctx: CompletionContext) -> bool:
                return bool(ctx.args) and ctx.args[0] in _names_after_subcommands

            def complete(self, ctx: CompletionContext) -> list[Completion]:
                subcmd = ctx.args[0] if ctx.args else ""
                names = self._cm.list_contexts()
                if subcmd == "kill":
                    names = [
                        n for n in names
                        if self._cm.contexts[n].process_slot
                        and self._cm.contexts[n].process_slot.is_alive()
                    ]
                return [
                    Completion(value=n)
                    for n in names
                    if n.startswith(ctx.prefix)
                ]

        @self.registry.command(
            name="context",
            help="Manage shell contexts: push, pop, switch, list, kill.",
            params=[
                arg("subcommand", nargs="?", default="",
                    completer=ChoiceCompleter(["push", "pop", "switch", "list", "kill"])),
                arg("name", nargs="?", default="",
                    completer=ContextNameCompleter(self.context_manager)),
            ],
        )
        def context_cmd(subcommand: str = "", name: str = ""):
            if not subcommand:
                ctx = self.context_manager.current()
                if ctx:
                    vars_str = f" {ctx.variables}" if ctx.variables else ""
                    print(f"Current: {ctx.name}{vars_str}")
                else:
                    print("No active context.")
                return

            if subcommand == "push":
                if not name:
                    print("Usage: context push <name>")
                    return
                if name in self.context_manager.contexts:
                    print(f"Context '{name}' already exists.")
                    return
                parent = self.context_manager.current()
                inherited = dict(parent.variables) if parent else {}
                self.context_manager.create(name, variables=inherited)
                self.context_manager.push(name)
                print(f"Pushed context '{name}'")

            elif subcommand == "pop":
                ctx = self.context_manager.current()
                if ctx is None:
                    print("No active context.")
                    return
                if len(self.context_manager.list_contexts()) <= 1:
                    print("Cannot remove the last context.")
                    return
                popped_name = ctx.name
                self.context_manager.pop()
                self.context_manager.remove(popped_name)
                prev = self.context_manager.current()
                if prev is None:
                    remaining = self.context_manager.list_contexts()
                    if remaining:
                        self.context_manager.switch(remaining[0])
                        prev = self.context_manager.current()
                if prev:
                    print(f"Popped '{popped_name}', now in '{prev.name}'")
                else:
                    print(f"Popped '{popped_name}'")

            elif subcommand == "switch":
                if not name:
                    print("Usage: context switch <name>")
                    return
                try:
                    self.context_manager.switch(name)
                except KeyError as e:
                    print(e)

            elif subcommand == "list":
                names = self.context_manager.list_contexts()
                if not names:
                    print("No contexts defined.")
                else:
                    current = self.context_manager.current_name
                    ordered = ([current] if current else []) + [n for n in names if n != current]
                    for n in ordered:
                        marker = "*" if n == current else " "
                        ctx = self.context_manager.contexts[n]
                        state = ctx.state.name.lower()
                        if state == "idle":
                            state_str = ""
                        elif state == "running" and ctx.process_slot and ctx.process_slot.argv:
                            cmd = " ".join(ctx.process_slot.argv)
                            state_str = f" (running: {cmd})"
                        else:
                            state_str = f" ({state})"
                        vars_str = f" {ctx.variables}" if ctx.variables else ""
                        print(f"  {marker} {n}{state_str}{vars_str}")

            elif subcommand == "kill":
                if not name:
                    print("Usage: context kill <name>")
                    return
                if name not in self.context_manager.contexts:
                    print(f"No context named '{name}'")
                    return
                target_ctx = self.context_manager.contexts[name]
                if target_ctx.process_slot and target_ctx.process_slot.is_alive():
                    target_ctx.process_slot.kill()
                    print(f"Sent SIGTERM to process in context '{name}'")
                else:
                    print(f"Context '{name}' has no running process.")

            else:
                print(f"Unknown subcommand: {subcommand}")

    def _load_user_config(self) -> None:
        config_path = Path.home() / ".cshell2" / "config.py"
        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(_DEFAULT_CONFIG_PATH.read_text())
            return

        import importlib.util
        sys.modules.pop("cshell2_user_config", None)
        spec = importlib.util.spec_from_file_location("cshell2_user_config", config_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules["cshell2_user_config"] = module
            try:
                spec.loader.exec_module(module)
            except KeyboardInterrupt:
                # VSCode and similar terminal integrations may inject a Ctrl+C
                # right after opening a terminal (to clear any in-progress input
                # before auto-activating a venv). If that lands during config
                # load — typically inside a slow import like boto3 — exit
                # cleanly so the user can re-run cshell2 once the terminal has
                # finished its startup dance, instead of crashing with a
                # traceback or starting up half-configured.
                print("Config load interrupted by Ctrl+C; exiting.", file=sys.stderr)
                sys.exit(130)
            except Exception as e:
                print(f"Error loading config: {e}", file=sys.stderr)

    _ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)")

    def _unset_variable(self, key: str) -> None:
        """Remove a variable via var_registry, or fall back to plain os.environ / context removal."""
        py_var = var_registry.get(key)
        if py_var is not None:
            py_var.unset()
            for env_key in py_var.env_keys:
                self.context_manager.unset_variable(env_key)
        else:
            self.context_manager.unset_variable(key)

    def _set_variable(self, key: str, value: str) -> None:
        """Dispatch a KEY=VALUE assignment through var_registry, or fall back to plain os.environ.

        When a registered Var handles the key its env_keys are registered with
        the context manager first (so original values are captured as the
        save/restore backup), then Var.set() is called to apply the change.
        """
        py_var = var_registry.get(key)
        if py_var is not None:
            for env_key in py_var.env_keys:
                self.context_manager.set_variable(env_key, os.environ.get(env_key, value))
            py_var.set(value)
            # Sync context's stored value to what set() actually wrote.
            ctx = self.context_manager.current()
            if ctx is not None:
                for env_key in py_var.env_keys:
                    ctx.variables[env_key] = os.environ.get(env_key, value)
        else:
            self.context_manager.set_variable(key, value)

    def _execute(self, line: str) -> None:
        seq = parse_line(expand_vars(line))
        last_exit = 0
        for op, pipeline in seq.items:
            if op == "&&" and last_exit != 0:
                continue
            if op == "||" and last_exit == 0:
                continue
            last_exit = self._execute_pipeline(pipeline)

    def _tokenize_stage(self, stage: Stage) -> list[str]:
        """Expand variables, tokenize, alias-expand, and glob-expand a stage's text."""
        tokens = tokenize(stage.text + " ")
        tokens = [os.path.expanduser(t) for t in tokens]
        tokens = self._expand_alias(tokens)
        return expand_globs(tokens)

    def _expand_alias(self, tokens: list[str]) -> list[str]:
        """Replace the first token with its alias expansion, if any.

        Aliases never chain — the expansion's own first token is not
        re-expanded — so cycles are impossible.
        """
        if not tokens:
            return tokens
        expansion = self.registry.get_alias(tokens[0])
        if expansion is None:
            return tokens
        expansion_tokens = tokenize(expansion)
        if not expansion_tokens:
            return tokens
        return expansion_tokens + tokens[1:]

    def _execute_pipeline(self, pipeline: Pipeline) -> int:
        """Execute a pipeline; return exit code of last stage."""
        stages = pipeline.stages
        if len(stages) == 1:
            return self._execute_stage(stages[0], stdin_fd=None, stdout_fd=None)

        # Multi-stage pipeline: connect with OS pipes.  External stages run
        # via subprocess.Popen; registered Python commands run in worker
        # threads that rebind sys.stdin/stdout/stderr to the pipe ends via
        # the thread-local routers installed in __init__.

        n = len(stages)
        pipe_fds: list[tuple[int, int]] = []
        for _ in range(n - 1):
            pipe_fds.append(os.pipe())

        # Workers list contains either subprocess.Popen instances or
        # _PyStageHandle objects (see _start_python_stage_thread).
        workers: list = []
        for idx, stage in enumerate(stages):
            tokens = self._tokenize_stage(stage)
            if not tokens:
                continue

            cmd = self.registry.get(tokens[0])
            if cmd is not None and not cmd.has_any_handler():
                cmd = None

            stdin_fd_pipe = pipe_fds[idx - 1][0] if idx > 0 else None
            stdout_fd_pipe = pipe_fds[idx][1] if idx < n - 1 else None

            # Resolve explicit redirects (override the pipe ends).
            stdin_file = stdout_file = None
            stderr_dst: object | None = None
            for redir in stage.redirects:
                if redir.kind == "<":
                    stdin_file = open(redir.target, "rb")
                elif redir.kind == ">":
                    stdout_file = open(redir.target, "wb")
                elif redir.kind == ">>":
                    stdout_file = open(redir.target, "ab")
                elif redir.kind == "2>":
                    stderr_dst = open(redir.target, "wb")
                elif redir.kind == "2>>":
                    stderr_dst = open(redir.target, "ab")
                elif redir.kind == "2>&1":
                    stderr_dst = subprocess.STDOUT

            stdin_pipe_used = stdin_fd_pipe is not None and stdin_file is None
            stdout_pipe_used = stdout_fd_pipe is not None and stdout_file is None
            is_py_stage = cmd is not None

            worker = None
            if is_py_stage:
                worker = self._start_python_stage_thread(
                    cmd=cmd,
                    args=tokens[1:],
                    stdin_fd=stdin_fd_pipe if stdin_pipe_used else None,
                    stdout_fd=stdout_fd_pipe if stdout_pipe_used else None,
                    stdin_file=stdin_file,
                    stdout_file=stdout_file,
                    stderr_dst=stderr_dst,
                )
            else:
                stdin_arg = stdin_file if stdin_file else stdin_fd_pipe
                stdout_arg = stdout_file if stdout_file else stdout_fd_pipe
                try:
                    worker = subprocess.Popen(
                        tokens,
                        stdin=stdin_arg,
                        stdout=stdout_arg,
                        stderr=stderr_dst,
                        env=dict(os.environ),
                        cwd=os.getcwd(),
                    )
                except FileNotFoundError:
                    print(f"cshell2: command not found: {tokens[0]}")
                except OSError as e:
                    print(f"cshell2: {e}")

            if worker is not None:
                workers.append(worker)

            # Drop the parent's reference to each pipe end the stage used.
            # For Popen the OS-level dup has already happened, so closing here
            # is correct.  For a Python-thread stage the worker thread owns
            # the fd via the TextIOWrapper passed to it and will close it
            # itself — so close here only if the stage *didn't* use that end
            # (because of an explicit redirect, or because the stage failed
            # to start at all).
            close_stdin_pipe = (
                idx > 0
                and stdin_fd_pipe is not None
                and (worker is None or not is_py_stage or not stdin_pipe_used)
            )
            close_stdout_pipe = (
                idx < n - 1
                and stdout_fd_pipe is not None
                and (worker is None or not is_py_stage or not stdout_pipe_used)
            )
            if close_stdin_pipe:
                os.close(stdin_fd_pipe)
            if close_stdout_pipe:
                os.close(stdout_fd_pipe)

            # Close redirect file objects we opened.  Popen has already dup'd
            # them; the Python-thread stage took ownership of them, so in
            # both cases the parent's copy is no longer needed — except when
            # the stage failed to start, in which case we must close them
            # ourselves to release the fd.
            if not is_py_stage or worker is None:
                if stdin_file:
                    stdin_file.close()
                if stdout_file:
                    stdout_file.close()
                if (
                    stderr_dst is not None
                    and stderr_dst is not subprocess.STDOUT
                    and hasattr(stderr_dst, "close")
                ):
                    try:
                        stderr_dst.close()
                    except Exception:
                        pass

        exit_code = 0
        try:
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.wait()
                    exit_code = w.exit_code or 0
                else:
                    w.wait()
                    exit_code = w.returncode or 0
        except KeyboardInterrupt:
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.interrupt()
                else:
                    try:
                        w.terminate()
                    except Exception:
                        pass
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.wait()
                else:
                    try:
                        w.wait()
                    except Exception:
                        pass
            exit_code = 130
        return exit_code

    def _start_python_stage_thread(
        self,
        *,
        cmd,
        args: list[str],
        stdin_fd: int | None,
        stdout_fd: int | None,
        stdin_file,
        stdout_file,
        stderr_dst,
    ) -> "_PyStageHandle":
        """Run a registered Python command as one stage of a pipeline.

        The thread takes ownership of *stdin_fd* / *stdout_fd* (raw OS pipe
        ends) or, when an explicit redirect is in play, the corresponding
        opened file object.  It binds them to thread-local
        sys.stdin/sys.stdout/sys.stderr for the duration of cmd.invoke().
        """
        # Decide which underlying object the thread owns.  Exactly one of
        # (stdin_fd, stdin_file) is set when this stage has any stdin source,
        # and similarly for stdout.
        in_obj = None
        if stdin_file is not None:
            in_obj = stdin_file
        elif stdin_fd is not None:
            in_obj = os.fdopen(stdin_fd, "rb", buffering=0, closefd=True)

        out_obj = None
        if stdout_file is not None:
            out_obj = stdout_file
        elif stdout_fd is not None:
            out_obj = os.fdopen(stdout_fd, "wb", buffering=0, closefd=True)

        err_obj = stderr_dst  # may be a file, "stdout" sentinel (subprocess.STDOUT), or None

        handle = _PyStageHandle(cmd_name=cmd.name)
        in_wrapper = io.TextIOWrapper(in_obj, encoding="utf-8", errors="replace") if in_obj is not None else None
        out_wrapper = io.TextIOWrapper(out_obj, encoding="utf-8", errors="replace", write_through=True) if out_obj is not None else None
        err_wrapper = None
        if err_obj is not None and err_obj is not subprocess.STDOUT:
            err_wrapper = io.TextIOWrapper(err_obj, encoding="utf-8", errors="replace", write_through=True)
        # Hand wrappers to the handle so interrupt() can close them.
        for w in (in_wrapper, out_wrapper, err_wrapper):
            if w is not None:
                handle._io_objs.append(w)

        def _target():
            _in_pipeline.flag = True
            try:
                if in_wrapper is not None:
                    sys.stdin.set_override(in_wrapper)
                if out_wrapper is not None:
                    sys.stdout.set_override(out_wrapper)
                if err_obj is subprocess.STDOUT:
                    sys.stderr.set_override(out_wrapper if out_wrapper is not None else sys.stdout)
                elif err_wrapper is not None:
                    sys.stderr.set_override(err_wrapper)

                try:
                    cmd.invoke(args)
                    handle.exit_code = 0
                except SystemExit as e:
                    # Don't propagate; an `exit | cat` should not kill the shell.
                    code = e.code
                    handle.exit_code = code if isinstance(code, int) else (1 if code else 0)
                except BrokenPipeError:
                    handle.exit_code = 0
                except KeyboardInterrupt:
                    handle.exit_code = 130
                except Exception as e:
                    if handle.interrupted:
                        # The parent closed our wrappers as part of Ctrl+C
                        # handling.  Any I/O the worker did afterward will
                        # raise (ValueError: closed file, or OSError) — that's
                        # expected, not an error to report.
                        handle.exit_code = 130
                    else:
                        print(f"{cmd.name}: error: {e}", file=sys.stderr)
                        traceback.print_exc()
                        handle.exit_code = 1
            finally:
                sys.stdin.clear_override()
                sys.stdout.clear_override()
                sys.stderr.clear_override()
                # Flush wrappers so downstream readers see all output before
                # the pipe closes.
                for w in (out_wrapper, err_wrapper):
                    if w is not None:
                        try:
                            w.flush()
                        except Exception:
                            pass
                # Close the wrappers (which closes the underlying fds/files).
                # Order matters: close output first so a reader pipe sees EOF.
                for w in (out_wrapper, err_wrapper, in_wrapper):
                    if w is not None:
                        try:
                            w.close()
                        except Exception:
                            pass
                _in_pipeline.flag = False
                handle.done.set()

        t = threading.Thread(target=_target, name=f"pipe-{cmd.name}", daemon=True)
        handle.thread = t
        t.start()
        return handle

    def _execute_stage(self, stage: Stage, stdin_fd, stdout_fd) -> int:
        """Execute a single stage (no pipe neighbours).

        stdin_fd / stdout_fd are file descriptors or None (meaning inherit terminal).
        Returns exit code.
        """
        tokens = self._tokenize_stage(stage)
        if not tokens:
            return 0

        # Pure-assignment line
        if all(self._ASSIGNMENT_RE.match(t) for t in tokens):
            for token in tokens:
                m = self._ASSIGNMENT_RE.match(token)
                self._set_variable(m.group(1), m.group(2))
            return 0

        command_name = tokens[0]
        args = tokens[1:]

        # Resolve redirections
        stdin_override = stdout_override = stderr_override = None
        for redir in stage.redirects:
            if redir.kind == "<":
                stdin_override = open(redir.target, "rb")
            elif redir.kind == ">":
                stdout_override = open(redir.target, "wb")
            elif redir.kind == ">>":
                stdout_override = open(redir.target, "ab")
            elif redir.kind == "2>":
                stderr_override = open(redir.target, "wb")
            elif redir.kind == "2>>":
                stderr_override = open(redir.target, "ab")
            elif redir.kind == "2>&1":
                stderr_override = "stdout"

        has_redirects = any([stdin_override, stdout_override, stderr_override])

        cmd = self.registry.get(command_name)
        # External recipes are stored as Command nodes too (for unified
        # completion), but have no Python handler anywhere — fall through to
        # the system command path so the real binary runs.
        if cmd is not None and not cmd.has_any_handler():
            cmd = None
        if cmd:
            if has_redirects:
                # Redirected Python command — run synchronously with overridden streams.
                old_stdout = sys.stdout
                old_stdin = sys.stdin
                old_stderr = sys.stderr
                try:
                    if stdout_override:
                        sys.stdout = io.TextIOWrapper(stdout_override)
                    if stdin_override:
                        sys.stdin = io.TextIOWrapper(stdin_override)
                    if stderr_override == "stdout":
                        sys.stderr = sys.stdout
                    elif stderr_override:
                        sys.stderr = io.TextIOWrapper(stderr_override)
                    cmd.invoke(args)
                except SystemExit:
                    raise
                except TypeError as e:
                    print(f"{command_name}: {e}")
                except Exception as e:
                    print(f"{command_name}: error: {e}")
                    traceback.print_exc()
                finally:
                    sys.stdout = old_stdout
                    sys.stdin = old_stdin
                    sys.stderr = old_stderr
                    for f in (stdout_override, stdin_override):
                        if f:
                            try:
                                f.close()
                            except Exception:
                                pass
                    if stderr_override and stderr_override != "stdout":
                        try:
                            stderr_override.close()
                        except Exception:
                            pass
            elif IS_WINDOWS:
                # Windows lacks the PTY-backed slot used for thread-based
                # context switching, so run the Python command synchronously.
                # passthrough_run/passthrough_input fall back to direct
                # subprocess.run/input since no slot is registered.
                self._run_python_command_sync(cmd, command_name, args)
            else:
                # Interactive Python command — run in a thread so Ctrl+] works.
                ctx = self.context_manager.current()
                slot = PythonCommandSlot(cmd, args)
                slot.start()
                result = self._enter_python_forwarding_mode(slot)
                if result == "switched":
                    slot.deactivate()
                    if ctx is not None:
                        ctx.process_slot = slot
                    self._handle_switch()
                elif result == "interrupted":
                    print(f"{command_name}: interrupted")
                else:
                    slot.deactivate()
                    exc = slot._exit_exception
                    if isinstance(exc, SystemExit):
                        raise exc
                    if exc is not None and not isinstance(exc, KeyboardInterrupt):
                        print(f"{command_name}: error: {exc}")
                        traceback.print_exc()
            return 0

        # External command
        if has_redirects:
            import subprocess
            stdin_arg = stdin_override or None
            stdout_arg = stdout_override or None
            if stderr_override == "stdout":
                stderr_arg = subprocess.STDOUT
            else:
                stderr_arg = stderr_override or None
            try:
                p = subprocess.run(
                    [command_name] + args,
                    stdin=stdin_arg,
                    stdout=stdout_arg,
                    stderr=stderr_arg,
                    env=dict(os.environ),
                    cwd=os.getcwd(),
                )
            except FileNotFoundError:
                print(f"cshell2: command not found: {command_name}")
                return 127
            except OSError as e:
                print(f"cshell2: {e}")
                return 1
            finally:
                for f in (stdin_override, stdout_override):
                    if f:
                        try:
                            f.close()
                        except Exception:
                            pass
                if stderr_override and stderr_override != "stdout":
                    try:
                        stderr_override.close()
                    except Exception:
                        pass
            return p.returncode
        else:
            self._execute_external(command_name, args)
            return 0

    def _run_python_command_sync(self, cmd, command_name: str, args: list[str]) -> None:
        """Invoke a Python command on the main thread (Windows path)."""
        try:
            cmd.invoke(args)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            print(f"{command_name}: interrupted")
        except TypeError as e:
            print(f"{command_name}: {e}")
        except Exception as e:
            print(f"{command_name}: error: {e}")
            traceback.print_exc()

    def _execute_external_windows(self, command_name: str, args: list[str]) -> None:
        """Run an external command on the real console (Windows path).

        Without ConPTY-based multiplexing, the child simply inherits the
        terminal's stdio. Commands that are cmd.exe builtins (dir, echo, cls,
        …) rather than real executables are retried via ``cmd /c``.
        """
        argv = [command_name] + args
        env = dict(os.environ)
        cwd = os.getcwd()
        try:
            subprocess.run(argv, env=env, cwd=cwd)
        except FileNotFoundError:
            try:
                subprocess.run(["cmd", "/c", *argv], env=env, cwd=cwd)
            except FileNotFoundError:
                print(f"cshell2: command not found: {command_name}")
            except OSError as e:
                print(f"cshell2: {e}")
        except OSError as e:
            print(f"cshell2: {e}")

    def _execute_external(self, command_name: str, args: list[str]) -> None:
        if IS_WINDOWS:
            self._execute_external_windows(command_name, args)
            return

        ctx = self.context_manager.current()

        slot = ProcessSlot()
        try:
            slot.start(
                argv=[command_name] + args,
                env=dict(os.environ),
                cwd=os.getcwd(),
            )
        except FileNotFoundError:
            print(f"cshell2: command not found: {command_name}")
            return
        except OSError as e:
            print(f"cshell2: {e}")
            return

        slot.activate()
        slot.replay_buffer()  # flush any output that arrived before activate()
        result = self._enter_forwarding_mode(slot)
        if result == "switched":
            if ctx is None:
                ctx = self.context_manager.current()
            ctx.process_slot = slot
            slot.deactivate()
            self._handle_switch()
        elif result == "exited":
            slot.deactivate()
            if ctx is not None:
                ctx.process_slot = None
            exit_code = slot.exit_code
            if exit_code and exit_code != 0:
                print(f"\n[Process exited with code {exit_code}]")

    def _enter_forwarding_mode(self, slot: ProcessSlot, force_redraw: bool = False) -> str:
        """Forward I/O between real terminal and subprocess PTY.

        Returns 'exited' if process finished, 'switched' if user pressed Ctrl+].
        """
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigint = signal.getsignal(signal.SIGINT)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)
        result = "exited"
        try:
            tty.setraw(fd)
            signal.signal(signal.SIGINT, signal.SIG_IGN)

            def on_resize(signum, frame):
                try:
                    size = os.get_terminal_size(fd)
                    slot.resize(size.lines, size.columns)
                except OSError:
                    pass

            signal.signal(signal.SIGWINCH, on_resize)

            if force_redraw:
                on_resize(None, None)

            while slot.is_alive():
                rlist, _, _ = select.select([fd], [], [], 0.1)
                if fd in rlist:
                    data = os.read(fd, 1024)
                    if not data:
                        break
                    if b"\x1d" in data:
                        idx = data.index(b"\x1d")
                        if idx > 0:
                            slot.write_stdin(data[:idx])
                        result = "switched"
                        break
                    slot.write_stdin(data)
            return result
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            if result == "switched":
                suspend_seq = slot.suspend_terminal_modes()
                if suspend_seq:
                    sys.stdout.write(suspend_seq)
                    sys.stdout.flush()

    def _enter_python_forwarding_mode(self, slot: PythonCommandSlot) -> str:
        """Monitor stdin while a Python command runs in a background thread.

        Sets the terminal to raw mode, activates the slot's stdout proxy
        (replaying any buffered output), then loops:
          • Ctrl+] (\\x1d) — return 'switched' so caller can store the slot
          • Ctrl+C (\\x03) — forwarded to a passthrough_run() subprocess if
            one is active (so e.g. SSH/SSM see the interrupt); otherwise
            inject KeyboardInterrupt into the command thread.
          • other keys    — forwarded to slot.write_stdin, which writes to
            a passthrough_run() PTY master if active (no-op otherwise).

        If the command thread enters passthrough_input(), the loop restores
        cooked terminal mode and stops reading stdin until the input() call
        returns.  That gives the slot thread direct, line-buffered access
        to the terminal for the prompt.

        Returns 'exited' when the thread finishes, 'switched' on Ctrl+].
        """
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigint = signal.getsignal(signal.SIGINT)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)
        result = "exited"

        def on_resize(signum, frame):
            try:
                size = os.get_terminal_size(fd)
                slot.resize(size.lines, size.columns)
            except OSError:
                pass

        try:
            tty.setraw(fd)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGWINCH, on_resize)
            # Replay any output buffered before raw mode was set
            slot.activate(raw_mode=True)

            while slot.is_alive():
                if slot._input_request.is_set():
                    # Hand stdin and cooked mode over to the slot thread for
                    # the duration of its input() call.
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
                    slot._input_released.set()
                    while slot.is_alive() and not slot._input_resume.is_set():
                        slot._input_resume.wait(timeout=0.1)
                    if not slot.is_alive():
                        break
                    tty.setraw(fd)
                    continue

                rlist, _, _ = select.select([fd], [], [], 0.1)
                if fd in rlist:
                    data = os.read(fd, 1024)
                    if not data:
                        break
                    if b"\x1d" in data:
                        result = "switched"
                        break
                    if b"\x03" in data and not slot._pty_active:
                        # No passthrough subprocess is running — interrupt
                        # the Python command itself.
                        slot.deactivate()
                        slot.kill()
                        result = "interrupted"
                        break
                    # Forward to slot.  When a passthrough_run() subprocess
                    # is active, this writes to its PTY master.  Otherwise
                    # write_stdin() is a no-op (the command isn't reading).
                    slot.write_stdin(data)
            return result
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGWINCH, old_sigwinch)

    _NEW_CTX_SENTINEL = "\x00new"

    def _show_switch_menu(self) -> tuple[str, bool] | None:
        """Show TUI context picker. Returns (name, is_new) or None on cancel."""
        contexts = self.context_manager.list_contexts()
        items = contexts + [self._NEW_CTX_SENTINEL]

        current = self.context_manager.current_name

        from .tui import InlineArgPrompt, InlinePicker

        def display_fn(name: str) -> str:
            if name == self._NEW_CTX_SENTINEL:
                return "+ new context"
            return ("* " if name == current else "  ") + name

        def meta_fn(name: str) -> str:
            if name == self._NEW_CTX_SENTINEL:
                return ""
            ctx = self.context_manager.contexts[name]
            slot = ctx.process_slot
            if slot and slot.is_alive() and slot.argv:
                parts = [os.path.basename(slot.argv[0])] + slot.argv[1:2]
                return " ".join(parts)
            return ""

        picker = InlinePicker(
            items,
            display_fn=display_fn,
            meta_fn=meta_fn,
            max_height=10,
            min_width=32,
            hide_cursor=True,
        )
        if current in contexts:
            picker._selected = contexts.index(current)

        selected = picker.run()

        if selected is None:
            return None

        if selected == self._NEW_CTX_SENTINEL:
            sys.stdout.write("\n")
            sys.stdout.flush()
            arg_prompt = InlineArgPrompt(label="new context name")
            name = arg_prompt.run()
            sys.stdout.write("\033[1A")
            sys.stdout.flush()
            if not name or name in self.context_manager.contexts:
                return None
            return (name, True)

        if selected == current:
            return None
        return (selected, False)

    def _resume_pty_slot(self, slot: ProcessSlot) -> None:
        """Restore terminal modes and re-activate a backgrounded PTY slot.

        Used both when resuming after a context switch and when the user
        cancels the switch picker.  Two strategies, picked by alt-screen
        state:
          • Alt-screen TUIs (vi, tfm, less): rely on the app's own
            redraw.  We force a SIGWINCH-driven repaint by wiggling the
            PTY size: (1, 1) then the real (rows, cols).  This guarantees
            ncurses sees a real resize event (not a no-op short-circuit)
            and triggers KEY_RESIZE → full clear+redraw.  Snapshot replay
            of the buffer is unsafe — paint commands are size-dependent
            and the deque-bounded history can be partial, leaving stale
            cells (selection markers, mis-positioned separators) that
            ncurses' shadow won't consider dirty.
          • Streaming output (logs, build): activate(replay_missed=True)
            atomically prints bytes that arrived while inactive and
            clears the missed-buffer under the buffer lock.
        """
        restore_seq = slot.restore_terminal_modes()
        if restore_seq:
            sys.stdout.write(restore_seq)
            sys.stdout.flush()
        if slot.terminal_modes.get("alt_screen", False):
            slot.activate()
            # Force the app to do a full clear+redraw.  The freshly-
            # restored alt-screen is blank, but the app's internal shadow
            # still matches what was on screen pre-suspend, so a passive
            # resume leaves the screen empty until the app repaints.
            #
            # Send Ctrl+L (\x0c, FF) — the universal TUI convention for
            # "force full redraw".  Bound by default in vim, nvim, nano,
            # less, mc, emacs, htop, etc.  Apps that don't handle it
            # (and aren't simply ignoring it) should — it's the standard
            # contract.  Wiggling the PTY size to fire SIGWINCH/KEY_RESIZE
            # works around non-conforming apps but introduces its own
            # artifacts (vim's per-column diff redraw confusion when the
            # file exceeds viewport height), so we no longer do that.
            slot.write_stdin(b"\x0c")
        else:
            slot.activate(replay_missed=True)

    def _handle_switch(self) -> bool:
        """Handle Ctrl+] switch request.

        Returns True to signal lineedit that it should exit (CONTEXT_CHANGED_SENTINEL)
        so the run() loop can take over — either to replay buffered output or to
        enter forwarding mode.  Returns False only when there is nothing pending in
        the target context (no process_slot at all).
        """
        ctx = self.context_manager.current()
        if ctx and ctx.process_slot:
            ctx.process_slot.deactivate()

        result = self._show_switch_menu()

        if result is None:
            # User cancelled.  Re-activate PTY slots so their reader thread can
            # stream output again.  PythonCommandSlots stay deactivated: their
            # buffered output will be replayed correctly (with raw_mode=True) the
            # next time _enter_python_forwarding_mode is called from run().
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                slot = ctx.process_slot
                if not isinstance(slot, PythonCommandSlot):
                    self._resume_pty_slot(slot)
            return False

        target_name, is_new = result

        if is_new:
            parent = self.context_manager.current()
            inherited = dict(parent.variables) if parent else {}
            self.context_manager.create(target_name, variables=inherited)
            self.context_manager.push(target_name)
        else:
            self.context_manager.switch(target_name)

        new_ctx = self.context_manager.current()
        if new_ctx and new_ctx.process_slot:
            # Don't activate the slot here — leave that to run()'s resume
            # path so it can choose between snapshot replay (TUI) and
            # missed-buffer flush (streaming) based on alt-screen state.
            # Returning True makes lineedit exit so run() takes over
            # immediately rather than waiting for the user to press Enter.
            return True
        return False

    def _background_count(self) -> int:
        """Count contexts with running processes (excluding current)."""
        current = self.context_manager.current_name
        count = 0
        for name, ctx in self.context_manager.contexts.items():
            if name != current and ctx.state == ContextState.RUNNING:
                count += 1
        return count

    def run(self) -> None:
        self._install_sigwinch_handler()
        print("cshell2 — type 'help' for available commands, 'exit' to quit.")
        while True:
            try:
                ctx = self.context_manager.current()

                if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                    slot = ctx.process_slot
                    if isinstance(slot, PythonCommandSlot):
                        # Resume a backgrounded Python command.
                        result = self._enter_python_forwarding_mode(slot)
                        slot.deactivate()
                        if result == "switched":
                            self._handle_switch()
                            continue
                        elif result == "interrupted":
                            ctx.process_slot = None
                            print(f"{slot.argv[0]}: interrupted")
                            continue
                        else:
                            exc = slot._exit_exception
                            ctx.process_slot = None
                            if isinstance(exc, SystemExit):
                                raise exc
                            if exc is not None and not isinstance(exc, KeyboardInterrupt):
                                print(f"\n[Python command error: {exc}]")
                            elif slot.exit_code and slot.exit_code != 0:
                                print(f"\n[Process exited with code {slot.exit_code}]")
                            continue
                    else:
                        # Resume a PTY subprocess.
                        self._resume_pty_slot(slot)
                        result = self._enter_forwarding_mode(slot, force_redraw=True)
                        slot.deactivate()
                        if result == "switched":
                            self._handle_switch()
                            continue
                        else:
                            exit_code = slot.exit_code
                            ctx.process_slot = None
                            if exit_code and exit_code != 0:
                                print(f"\n[Process exited with code {exit_code}]")
                            continue

                if ctx and ctx.process_slot and not ctx.process_slot.is_alive():
                    slot = ctx.process_slot
                    slot.replay_buffer()
                    exit_code = slot.exit_code
                    ctx.process_slot = None
                    if isinstance(slot, PythonCommandSlot) and exit_code == 130:
                        print(f"{slot.argv[0]}: killed")
                    elif exit_code and exit_code != 0:
                        print(f"\n[Process exited with code {exit_code}]")

                # Collect the primary line (history managed here, not inside the editor).
                text = self._line_editor.prompt(add_to_history=False)
                if text == SWITCH_SENTINEL:
                    self._handle_switch()
                    continue
                if text == CONTEXT_CHANGED_SENTINEL:
                    continue

                # Handle backslash line continuation: keep prompting with "> "
                # until a line that does NOT end with an unescaped backslash.
                # Ctrl+C propagates as KeyboardInterrupt and abandons the command.
                # A context-switch (Ctrl+]) during continuation also abandons it.
                full_text = text
                while _is_continuation(full_text):
                    partial = _strip_continuation(full_text)
                    cont = self._line_editor.prompt(prompt_str="> ", add_to_history=False)
                    if cont in (SWITCH_SENTINEL, CONTEXT_CHANGED_SENTINEL):
                        if cont == SWITCH_SENTINEL:
                            self._handle_switch()
                        full_text = ""
                        break
                    full_text = partial + cont

                if full_text.strip():
                    self._line_editor.add_to_history(full_text)
                    self._execute(full_text.strip())
            except KeyboardInterrupt:
                continue
            except EOFError:
                print("\nexit")
                running = self._running_contexts()
                if running and not self._confirm_exit(running):
                    continue
                break
            except SystemExit:
                break

    def _running_contexts(self) -> list[tuple[str, list[str]]]:
        return [
            (name, ctx.process_slot.argv)
            for name, ctx in self.context_manager.contexts.items()
            if ctx.process_slot and ctx.process_slot.is_alive()
        ]

    def _confirm_exit(self, running: list[tuple[str, list[str]]]) -> bool:
        print(f"There {'is' if len(running) == 1 else 'are'} {len(running)} context(s) with running processes:")
        for name, argv in running:
            print(f"  {name}: {' '.join(argv)}")
        try:
            answer = passthrough_input("Exit anyway? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")

    def _install_sigwinch_handler(self) -> None:
        # No SIGWINCH on Windows; live-process resize forwarding is part of the
        # PTY multiplexing path, which is POSIX-only.
        if not terminal.HAS_SIGWINCH:
            return

        def on_resize(signum, frame):
            ctx = self.context_manager.current()
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                try:
                    rows, cols = os.get_terminal_size()
                    ctx.process_slot.resize(rows, cols)
                except OSError:
                    pass

        signal.signal(signal.SIGWINCH, on_resize)
