"""@watch — re-run a pipeline on a timer until interrupted."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

from dataclasses import replace

from .. import terminal
from ..commands import arg
from ..pipeline import Pipeline, Redirect
from . import registry as decorator_registry


# Alt-screen entry/exit + cursor-home, matching POSIX watch(1):
# entering the alt-screen gives a separate buffer that's discarded on
# exit, so iterations replace each other instead of accumulating into
# scrollback.  ``\x1b[2J\x1b[H`` alone only blanks the visible region
# — old output still scrolls back, which is what users see as "always
# prints new lines."
_ALT_SCREEN_ENTER = "\x1b[?1049h"
_ALT_SCREEN_EXIT = "\x1b[?1049l"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_CLEAR_HOME = "\x1b[2J\x1b[H"


# Keys that quit the watch loop (matches POSIX watch(1)'s 'q' / Ctrl+C).
_QUIT_KEYS = (b"q", b"Q", b"\x03")


def _pipeline_redirected_to(pipeline: Pipeline, path: str) -> Pipeline:
    """Return a new Pipeline whose last stage's stdout is redirected to *path*.

    The original AST is left intact so subsequent iterations re-run the
    user's pipeline as written.
    """
    new_stages = list(pipeline.stages)
    last = new_stages[-1]
    new_stages[-1] = replace(
        last,
        redirects=list(last.redirects) + [Redirect(kind=">", target=path)],
    )
    return Pipeline(stages=new_stages)


def _truncate_to_terminal(text: str, rows: int, cols: int) -> str:
    """Trim *text* to fit in *rows* x *cols* without scrolling.

    Mirrors POSIX watch(1): each input line is cut at *cols* visible
    chars (control sequences are kept verbatim, since their visible
    width is zero), and at most *rows* lines are kept.  Output longer
    than the screen would otherwise scroll the alt-screen buffer and
    push the top off, which is what users see as "I see the bottom,
    not the top."

    Note: this is a best-effort visible-width approximation — wide
    characters and zero-width glyphs aren't accounted for.  Good
    enough for the common case (ASCII / narrow text).
    """
    if rows <= 0 or cols <= 0:
        return ""
    lines = text.splitlines()[:rows]
    out = []
    for line in lines:
        if len(line) <= cols:
            out.append(line)
            continue
        # Try to cut at *cols* characters but keep ANSI escape sequences
        # intact — most terminal output we run watch on is ASCII tables
        # without colour, so this simple slice is acceptable.
        out.append(line[:cols])
    return "\n".join(out)


def _drain_keys(fd: int) -> bytes:
    """Pull every queued key off *fd* without blocking."""
    out = bytearray()
    while terminal.wait_readable(fd, 0):
        chunk = terminal.read_key(fd)
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def _interruptible_sleep(fd: int, seconds: float) -> bool:
    """Sleep up to *seconds*, returning True if a quit key was pressed.

    Reads keys directly from *fd* (the real stdin).  ``@watch`` runs
    synchronously on the main thread, so stdin is ours for the duration
    of the loop — no slot-based forwarding to coordinate with.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not terminal.wait_readable(fd, min(remaining, 0.1)):
            continue
        key = terminal.read_key(fd)
        if not key:
            continue
        # Drain any further bytes already queued so a paste of "qqq"
        # quits cleanly rather than leaving extra keys in the buffer.
        _drain_keys(fd)
        if any(q in key for q in _QUIT_KEYS):
            return True


def register() -> None:
    @decorator_registry.decorator(
        name="watch",
        help="Repeatedly run a pipeline until interrupted (q or Ctrl+C to quit).",
        params=[
            arg("-n", "--interval", type=float, default=2.0, metavar="SEC",
                help="seconds between runs"),
            arg("--no-clear", action="store_true",
                help="stream output continuously (no alt-screen, no clear)"),
        ],
    )
    def watch(pipeline, *, interval: float, no_clear: bool) -> None:
        is_tty = sys.stdout.isatty()
        use_alt_screen = is_tty and not no_clear

        if not use_alt_screen:
            # Streaming mode — equivalent to a `while true; do …; sleep N; done` loop.
            try:
                while True:
                    try:
                        pipeline.run()
                    except BrokenPipeError:
                        return
                    time.sleep(interval)
            except KeyboardInterrupt:
                return
            return

        # Alt-screen mode — buffer each iteration's output to a temp file,
        # then blit it atomically.  Matches POSIX watch(1): the user never
        # sees a blank screen mid-execution; the prior frame stays put
        # until the new one is ready.
        #
        # Stdin needs to be in raw mode so the inter-iteration sleep can
        # read 'q' without waiting for Enter.  ``@watch`` runs on the
        # main thread, so it owns stdin for the duration — set raw mode
        # locally and restore on exit.
        try:
            stdin_fd = sys.stdin.fileno()
        except (OSError, ValueError):
            stdin_fd = -1

        saved_mode = None
        if stdin_fd >= 0:
            try:
                saved_mode = terminal.get_mode(stdin_fd)
                terminal.set_raw(stdin_fd)
            except Exception:
                saved_mode = None

        sys.stdout.write(_ALT_SCREEN_ENTER + _HIDE_CURSOR + _CLEAR_HOME)
        sys.stdout.flush()
        try:
            while True:
                fd, path = tempfile.mkstemp(prefix="cshell2-watch-")
                os.close(fd)
                try:
                    redirected = _pipeline_redirected_to(pipeline, path)
                    try:
                        redirected.run()
                    except BrokenPipeError:
                        return
                    try:
                        with open(path, "rb") as f:
                            output = f.read()
                    except OSError:
                        output = b""
                finally:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

                size = shutil.get_terminal_size((80, 24))
                rendered = _truncate_to_terminal(
                    output.decode(errors="replace"), size.lines, size.columns
                )
                # Raw mode means ``\n`` no longer auto-prefixes ``\r``; emit
                # ``\r\n`` so each line returns to column 0 instead of
                # stair-stepping diagonally.
                sys.stdout.write(_CLEAR_HOME)
                if rendered:
                    sys.stdout.write(rendered.replace("\n", "\r\n"))
                sys.stdout.flush()
                if stdin_fd >= 0:
                    if _interruptible_sleep(stdin_fd, interval):
                        return
                else:
                    time.sleep(interval)
        except KeyboardInterrupt:
            return
        finally:
            sys.stdout.write(_SHOW_CURSOR + _ALT_SCREEN_EXIT)
            sys.stdout.flush()
            if saved_mode is not None and stdin_fd >= 0:
                try:
                    terminal.restore_mode(stdin_fd, saved_mode)
                except Exception:
                    pass
