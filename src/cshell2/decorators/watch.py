"""@watch — re-run a pipeline on a timer until interrupted."""

from __future__ import annotations

import os
import sys
import tempfile
import time

from dataclasses import replace

from ..commands import arg
from ..pipeline import Pipeline, Redirect
from . import registry as decorator_registry


# Alt-screen entry/exit + cursor-home, matching POSIX watch(1):
# entering the alt-screen gives a separate buffer that's discarded on
# exit, so iterations replace each other instead of accumulating into
# scrollback.  ``\x1b[2J\x1b[H`` alone only blanks the visible region
# — old output still scrolls back, which is what users see as "always
# prints new lines."
_ALT_SCREEN_ENTER = "\x1b[?1049h\x1b[H"
_ALT_SCREEN_EXIT = "\x1b[?1049l"
_CLEAR_HOME = "\x1b[2J\x1b[H"


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


def register() -> None:
    @decorator_registry.decorator(
        name="watch",
        help="Repeatedly run a pipeline until interrupted.",
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
        sys.stdout.write(_ALT_SCREEN_ENTER)
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

                # Write through the text stream (not sys.stdout.buffer) so
                # the slot's _StdoutProxy applies its raw-mode \n→\r\n
                # translation.  Writing raw bytes via .buffer would skip
                # the proxy entirely and the cursor would only move down,
                # never back to column 0.
                sys.stdout.write(_CLEAR_HOME)
                if output:
                    sys.stdout.write(output.decode(errors="replace"))
                sys.stdout.flush()
                time.sleep(interval)
        except KeyboardInterrupt:
            return
        finally:
            sys.stdout.write(_ALT_SCREEN_EXIT)
            sys.stdout.flush()
