"""@watch — re-run a pipeline on a timer until interrupted."""

from __future__ import annotations

import sys
import time

from ..commands import arg
from . import registry as decorator_registry


def register() -> None:
    @decorator_registry.decorator(
        name="watch",
        help="Repeatedly run a pipeline until interrupted.",
        params=[
            arg("-n", "--interval", type=float, default=2.0, metavar="SEC",
                help="seconds between runs"),
            arg("--no-clear", action="store_true",
                help="don't clear screen between runs"),
        ],
    )
    def watch(pipeline, *, interval: float, no_clear: bool) -> None:
        try:
            while True:
                if not no_clear and sys.stdout.isatty():
                    sys.stdout.write("\x1b[2J\x1b[H")
                    sys.stdout.flush()
                try:
                    pipeline.run()
                except BrokenPipeError:
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            return
