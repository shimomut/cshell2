"""Completion recipe for watch — delegates to the wrapped command's completer.

Layout::

    watch [watch-flags...] <command> [command-args...]
                            ^^^^^^^^^^^^^^^^^^^^^^^^^
                            completion for these is dispatched
                            to the wrapped command's completers

When the cursor is past the wrapped command name, the recipe pretends the
shell is completing ``<command> [command-args...]`` directly: it invokes
the same dispatch chain (tree resolution, options completer, positional
completers, cobra/argcomplete fallbacks, file fallback) as the shell's
top-level dispatcher.
"""

from __future__ import annotations

import shutil

from ..commands import arg, flag_args, get_positional_completer, registry as command_registry
from ..completion import (
    CommandNameCompleter,
    Completer,
    Completion,
    CompletionContext,
    FileCompleter,
)

WATCH_OPTIONS: dict[str, str] = {
    "-b": "beep on non-zero exit",
    "--beep": "beep on non-zero exit",
    "-c": "interpret ANSI color/style sequences",
    "--color": "interpret ANSI color/style sequences",
    "-C": "do not interpret ANSI color/style sequences",
    "--no-color": "do not interpret ANSI color/style sequences",
    "-d": "highlight differences between updates",
    "--differences": "highlight differences between updates",
    "-e": "freeze on command error and exit on key press",
    "--errexit": "freeze on command error and exit on key press",
    "-g": "exit when output changes",
    "--chgexit": "exit when output changes",
    "-n": "interval between updates (seconds)",
    "--interval": "interval between updates (seconds)",
    "-p": "execute interval seconds after previous run, not after",
    "--precise": "execute interval seconds after previous run, not after",
    "-t": "turn off the header",
    "--no-title": "turn off the header",
    "-w": "do not wrap long lines",
    "--no-wrap": "do not wrap long lines",
    "-x": "pass command to exec instead of 'sh -c'",
    "--exec": "pass command to exec instead of 'sh -c'",
    "-h": "show help",
    "--help": "show help",
    "-v": "show version",
    "--version": "show version",
}

WATCH_ARGS: dict[str, str] = {
    "-n": "SECONDS",
    "--interval": "SECONDS",
}

# Set of flags that consume the following token as a value.  Used to walk
# past watch's own flags when locating the wrapped command.
_VALUE_TAKING = {"-n", "--interval"}


def _split_at_command(args: list[str]) -> tuple[list[str], list[str]]:
    """Split *args* into (watch_args, [command, *command_args]).

    Walks past watch's flags and their values; returns the position at
    which a non-flag token first appears.  If no command has been typed
    yet, returns (args, []).
    """
    i = 0
    while i < len(args):
        token = args[i]
        if not token.startswith("-"):
            return args[:i], args[i:]
        if token in _VALUE_TAKING:
            i += 2
        elif "=" in token and token.split("=", 1)[0] in _VALUE_TAKING:
            i += 1
        else:
            i += 1
    return args, []


class _WatchDispatcher(Completer):
    """Completer that delegates to the wrapped command's own completion.

    The shell asks the watch recipe for a completer at each positional
    index.  This dispatcher receives ``ctx`` for that position and
    decides whether to complete the wrapped command name (when no command
    has been typed) or to forward the request as if the user were
    completing the wrapped command directly.
    """

    def __init__(self) -> None:
        self._command_name_completer = CommandNameCompleter(command_registry)
        self._file_completer = FileCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        watch_args, inner = _split_at_command(ctx.args)

        if not inner:
            # Cursor is at the position where the wrapped command name goes.
            return self._command_name_completer.complete(ctx)

        # Wrapped command has been typed; dispatch as if the user were
        # completing "<command> [args...]" at the top level.
        inner_command = inner[0]
        inner_preceding = inner[1:]
        inner_ctx = CompletionContext(
            command=inner_command,
            args=inner_preceding,
            arg_index=len(inner_preceding),
            prefix=ctx.prefix,
            line=ctx.line,
            shell_context=ctx.shell_context,
        )
        return self._dispatch_inner(inner_command, inner_ctx)

    def _dispatch_inner(self, command_name: str, ctx: CompletionContext) -> list[Completion]:
        cmd = command_registry.get(command_name)
        completers_dict = cmd.completers if cmd else None

        if completers_dict:
            options_completer = completers_dict.get(None)

            # Flag value: typing "<cmd> -X <TAB>" shows the value hint or its
            # dedicated completer.
            if (options_completer and ctx.args and not ctx.prefix.startswith("-")
                    and hasattr(options_completer, "get_preceding_flag_hint")):
                hint_info = options_completer.get_preceding_flag_hint(ctx)
                if hint_info:
                    flag, arg_hint, description, value_completer = hint_info
                    if value_completer:
                        return value_completer.complete(ctx)
                    return [Completion(
                        value=flag,
                        display=f"<{arg_hint}>",
                        description=description,
                        arg_hint=arg_hint,
                        is_arg_hint=True,
                    )]

            # Options when prefix starts with "-".
            if options_completer and ctx.prefix.startswith("-"):
                if options_completer.should_activate(ctx):
                    return options_completer.complete(ctx)

            # Positional fallback.
            pos_idx = _inner_positional_index(ctx.args, options_completer)
            positional_completer = get_positional_completer(completers_dict, pos_idx)
            if positional_completer and positional_completer.should_activate(ctx):
                results = positional_completer.complete(ctx)
                if results:
                    return results

        # Fall back to plain file completion when nothing else applies.
        return self._file_completer.complete(ctx)


def _inner_positional_index(args: list[str], options_completer) -> int:
    """Mirror of shell._positional_index, duplicated here to avoid a circular
    import (cshell2.shell imports the recipe loader transitively).
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


def register() -> None:
    if shutil.which("watch") is None:
        return
    command_registry.command(
        "watch",
        help="execute a program periodically, showing output",
        params=[
            arg("command", nargs="*", help="command to run repeatedly",
                completer=_WatchDispatcher()),
            *flag_args(WATCH_OPTIONS, values=WATCH_ARGS),
        ],
    )
