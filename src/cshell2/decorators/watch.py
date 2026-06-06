"""@watch — re-run a pipeline on a timer until interrupted."""

from __future__ import annotations

import sys
import time

from ..commands import arg
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


def register() -> None:
    @decorator_registry.decorator(
        name="watch",
        help="Repeatedly run a pipeline until interrupted.",
        params=[
            arg("-n", "--interval", type=float, default=2.0, metavar="SEC",
                help="seconds between runs"),
            arg("--no-clear", action="store_true",
                help="don't clear screen between runs (also disables alt-screen)"),
        ],
    )
    def watch(pipeline, *, interval: float, no_clear: bool) -> None:
        is_tty = sys.stdout.isatty()
        use_alt_screen = is_tty and not no_clear
        if use_alt_screen:
            sys.stdout.write(_ALT_SCREEN_ENTER)
            sys.stdout.flush()
        try:
            while True:
                if use_alt_screen:
                    sys.stdout.write(_CLEAR_HOME)
                    sys.stdout.flush()
                try:
                    pipeline.run()
                except BrokenPipeError:
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            return
        finally:
            if use_alt_screen:
                sys.stdout.write(_ALT_SCREEN_EXIT)
                sys.stdout.flush()
