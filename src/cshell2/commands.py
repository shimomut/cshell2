"""Command registry, @command decorator, CmdParser, and arg() descriptor."""

from __future__ import annotations

import argparse
import inspect
import sys
from dataclasses import dataclass, field
from typing import Callable

from .completion import Completer, ChoiceCompleter, OptionsCompleter


class _HelpOrError(Exception):
    """Internal sentinel raised by CmdParser to avoid sys.exit()."""


class CmdParser(argparse.ArgumentParser):
    """ArgumentParser that is safe to use inside cshell2 commands.

    Standard ``ArgumentParser`` calls ``sys.exit()`` on ``--help`` and on
    parse errors, which would terminate the shell.  ``CmdParser`` intercepts
    both and returns ``None`` from :meth:`parse_args` instead — after printing
    the help or error message exactly as argparse normally would.

    Argparse handles combined short boolean flags automatically (``-nv`` is
    treated as ``-n -v``), so commands that expose cshell2's multi-select
    option TUI work without any extra effort.

    Typical usage inside a command function::

        def deploy(*args):
            parser = CmdParser("deploy")
            parser.add_argument("environment", choices=["prod", "staging", "dev"])
            parser.add_argument("-n", "--dry-run", action="store_true")
            parser.add_argument("-t", "--timeout", type=int, default=60)
            ns = parser.parse_args(args)
            if ns is None:
                return          # error or --help already printed
            # use ns.environment, ns.dry_run, ns.timeout, ...
    """

    def exit(self, status: int = 0, message: str | None = None) -> None:
        # Called by --help and --version; don't let it kill the shell.
        if message:
            print(message, end="", file=sys.stderr)
        raise _HelpOrError()

    def error(self, message: str) -> None:
        # Called for parse errors (missing args, unknown flags, wrong types).
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise _HelpOrError()

    def parse_args(self, args=None, namespace=None):
        try:
            return super().parse_args(args, namespace)
        except _HelpOrError:
            return None


@dataclass
class Arg:
    """Descriptor for one positional argument or flag.

    Created with :func:`arg`.  ``kwargs`` are forwarded verbatim to
    ``argparse.ArgumentParser.add_argument()``; ``completer`` is a
    cshell2-specific completion hint that is consumed by the registry and
    never passed to argparse.
    """
    names: tuple[str, ...]
    kwargs: dict
    completer: Completer | None = None


def arg(*names: str, completer: Completer | None = None, **kwargs) -> Arg:
    """Declare one positional argument or flag for a ``params=`` command.

    Keyword arguments mirror ``argparse.add_argument()`` exactly, plus one
    extra cshell2-specific keyword:

    ``completer``
        A :class:`~cshell2.completion.Completer` instance used for TAB
        completion of this argument's *value*.

        - For a **positional arg**, it completes the arg itself.  If omitted
          and ``choices=`` is set, a :class:`ChoiceCompleter` is derived
          automatically.
        - For a **value-taking flag** (``-t``, ``--timeout``, …), it
          completes the value the user types after the flag.
        - Ignored for **boolean flags** (``action="store_true"`` etc.).

    Examples::

        arg("environment", choices=["prod", "staging", "dev"])
        arg("instance",    completer=CallbackCompleter(fetch_instances))
        arg("-n", "--dry-run", action="store_true", help="skip execution")
        arg("-t", "--timeout", type=int, default=60, metavar="SECONDS",
            help="timeout in seconds",
            completer=ChoiceCompleter(["30", "60", "120", "300"]))

    The resulting :class:`Arg` is passed to ``@registry.command(params=[…])``.
    The registry derives both the argparse parser **and** the TAB-completion
    dict from the same list, so no separate ``completers=`` is needed.
    """
    return Arg(names=names, kwargs=kwargs, completer=completer)


# argparse actions that consume a following value token
_VALUE_ACTIONS = {"store", "append", "extend"}


def _build_completers(params: list[Arg]) -> dict[int | None, Completer]:
    """Derive a completers dict from a list of :class:`Arg` descriptors.

    Rules:

    * **Positional arg** — if ``completer=`` is set, use it; otherwise if
      ``choices=`` is set derive a :class:`ChoiceCompleter` automatically.
      Keyed by zero-based positional index.

    * **Flag** — all flag names are collected into an
      :class:`OptionsCompleter` under the ``None`` key.  ``help=`` text
      becomes the description shown in the completion menu.  Value-taking
      flags (action="store" / "append" / "extend") are also registered in
      the ``args=`` dict of the ``OptionsCompleter`` with their ``metavar``
      and optional ``completer`` for the value.
    """
    all_options: dict[str, str] = {}
    args_hints: dict[str, str | tuple] = {}
    pos_completers: dict[int, Completer] = {}
    pos_idx = 0

    for a in params:
        is_flag = a.names[0].startswith("-")

        if not is_flag:
            # ── positional arg ────────────────────────────────────────────
            comp = a.completer
            if comp is None and "choices" in a.kwargs:
                comp = ChoiceCompleter(list(a.kwargs["choices"]))
            if comp is not None:
                pos_completers[pos_idx] = comp
            pos_idx += 1

        else:
            # ── flag ──────────────────────────────────────────────────────
            desc = a.kwargs.get("help", "")
            for name in a.names:
                all_options[name] = desc

            action = a.kwargs.get("action", "store")
            if action in _VALUE_ACTIONS:
                # Derive metavar from --long-name → LONG_NAME, or metavar=
                long_names = [n for n in a.names if n.startswith("--")]
                dest = (long_names[0].lstrip("-").replace("-", "_")
                        if long_names else a.names[0].lstrip("-"))
                metavar = a.kwargs.get("metavar", dest.upper())
                hint: str | tuple = (metavar, a.completer) if a.completer else metavar
                for name in a.names:
                    args_hints[name] = hint

    result: dict[int | None, Completer] = {}
    if all_options:
        result[None] = OptionsCompleter(all_options, args=args_hints or None)
    result.update(pos_completers)
    return result


def _build_usage(cmd_name: str, params: list[Arg]) -> str:
    """Generate a compact usage line from a params list.

    Format mirrors conventional shell usage notation:

    * Required positional → ``<name>``
    * Optional positional (``nargs="?"`` or ``"*"``) → ``[name]``
    * Boolean flag → ``[-n]``  (shortest form)
    * Value-taking flag → ``[-n METAVAR]``  (shortest form + metavar)
    """
    parts = [cmd_name]
    for a in params:
        is_flag = a.names[0].startswith("-")
        if not is_flag:
            is_optional = a.kwargs.get("nargs") in ("?", "*")
            dest = a.names[0]
            parts.append(f"[{dest}]" if is_optional else f"<{dest}>")
        else:
            short = next((n for n in a.names if len(n) == 2), a.names[0])
            action = a.kwargs.get("action", "store")
            if action in _VALUE_ACTIONS:
                long_names = [n for n in a.names if n.startswith("--")]
                dest = (long_names[0].lstrip("-").replace("-", "_")
                        if long_names else a.names[0].lstrip("-"))
                metavar = a.kwargs.get("metavar", dest.upper())
                parts.append(f"[{short} {metavar}]")
            else:
                parts.append(f"[{short}]")
    return "Usage: " + " ".join(parts)


def _effective_description(help: str | None, func: Callable) -> str:
    """Return the description string: explicit *help* wins, then the docstring."""
    if help is not None:
        return help
    return inspect.getdoc(func) or ""


def _build_help_text(
    help: str | None,
    func: Callable,
    cmd_name: str,
    params: list[Arg] | None,
) -> str:
    """Assemble the full help text stored on a Command.

    Structure when both *help* and *params* are present::

        <description>

        Usage: cmd <pos> [opt] [-f] [-v VAL]

    The first line is always the short description (used in command listings).
    When neither *help* nor *params* is given, falls back to the function
    signature as a minimal hint (legacy behaviour).
    """
    desc = _effective_description(help, func)
    if params is not None:
        usage = _build_usage(cmd_name, params)
        return (desc + "\n\n" + usage).strip() if desc else usage
    return desc or _signature_help(func, cmd_name)


def _signature_help(func: Callable, cmd_name: str) -> str:
    """Generate a ``Usage: cmd_name [args]`` hint from the function signature.

    Shown as the fallback help text when a command has no docstring.
    Required parameters are shown as ``<name>``, optional ones as ``[name]``,
    and ``*args`` as ``[args...]``.  Returns an empty string when the signature
    cannot be determined.
    """
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return ""

    parts: list[str] = []
    for param_name, param in sig.parameters.items():
        if param.kind == param.VAR_POSITIONAL:
            parts.append(f"[{param_name}...]")
        elif param.kind == param.VAR_KEYWORD:
            pass  # **kwargs — not useful to surface in usage
        elif param.default is inspect.Parameter.empty:
            parts.append(f"<{param_name}>")
        else:
            parts.append(f"[{param_name}]")

    usage = " ".join([cmd_name] + parts)
    return f"Usage: {usage}"


@dataclass
class Command:
    name: str
    func: Callable
    completers: dict[int | None, Completer] = field(default_factory=dict)
    help_text: str = ""
    params: list[Arg] | None = None
    description: str = ""  # raw description line (from help=), used as CmdParser description

    def invoke(self, args: list[str] | tuple[str, ...]) -> None:
        """Dispatch a command invocation.

        When ``params`` is set the registry auto-builds a :class:`CmdParser`,
        parses *args*, and calls ``func(**vars(ns))``.  The function receives
        typed, validated keyword arguments and never touches raw tokens.

        When ``params`` is ``None`` (the default) the original ``func(*args)``
        behaviour is preserved for full backward compatibility.
        """
        if self.params is not None:
            parser = CmdParser(self.name, description=self.description or None)
            for a in self.params:
                parser.add_argument(*a.names, **a.kwargs)
            ns = parser.parse_args(args)
            if ns is None:
                return  # error or --help already printed by CmdParser
            self.func(**vars(ns))
        else:
            self.func(*args)


class CommandRegistry:
    def __init__(self):
        self._commands: dict[str, Command] = {}
        self._external_completers: dict[str, dict[int | None, Completer]] = {}
        self._builtin_names: set[str] = set()

    def command(
        self,
        name: str | None = None,
        completers: dict[int | None, Completer] | None = None,
        params: list[Arg] | None = None,
        help: str | None = None,
    ):
        """Decorator to register a Python function as a shell command.

        *help* is the shell-facing description shown by the ``help`` command.
        The function's docstring is **not** used; put all user-visible text in
        *help* instead (the docstring is still fine for Python-level docs).

        When *params* is provided the registry auto-generates a ``Usage:``
        line and appends it after the *help* text::

            @registry.command(
                name="greet",
                help="Greet a person by name.",
                params=[
                    arg("name"),
                    arg("-u", "--upper", action="store_true", help="uppercase"),
                ],
            )
            def greet(name, upper):
                print(name.upper() if upper else name)

        Without *params* the decorated function receives raw tokens as
        positional ``*args`` (original behaviour, fully backward-compatible).
        """
        def decorator(func: Callable) -> Callable:
            cmd_name = name or func.__name__
            derived = _build_completers(params) if params else {}
            merged = {**derived, **(completers or {})}
            cmd = Command(
                name=cmd_name,
                func=func,
                completers=merged,
                help_text=_build_help_text(help, func, cmd_name, params),
                params=params,
                description=_effective_description(help, func),
            )
            self._commands[cmd_name] = cmd
            return func
        return decorator

    def register(
        self,
        func: Callable,
        name: str | None = None,
        completers: dict[int | None, Completer] | None = None,
        params: list[Arg] | None = None,
        help: str | None = None,
    ) -> None:
        """Imperative registration (alternative to decorator)."""
        cmd_name = name or func.__name__
        derived = _build_completers(params) if params else {}
        merged = {**derived, **(completers or {})}
        cmd = Command(
            name=cmd_name,
            func=func,
            completers=merged,
            help_text=_build_help_text(help, func, cmd_name, params),
            params=params,
            description=_effective_description(help, func),
        )
        self._commands[cmd_name] = cmd

    def register_external_completers(
        self,
        command_name: str,
        completers: dict[int | None, Completer],
    ) -> None:
        """Register completers for an external (system) command.

        Use ``None`` as a key for an options completer that activates whenever
        the user types a ``-``-prefixed token at any argument position:

            registry.register_external_completers("ls", {
                None: OptionsCompleter({"-l": "long format", ...}),
                0: FileCompleter(),
            })
        """
        self._external_completers[command_name] = completers

    def get_external_completers(self, command_name: str) -> dict[int | None, Completer] | None:
        return self._external_completers.get(command_name)

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def list_commands(self) -> list[str]:
        return list(self._commands.keys())

    def has(self, name: str) -> bool:
        return name in self._commands

    def mark_builtins(self) -> None:
        """Snapshot current commands as builtins (won't be removed on reload)."""
        self._builtin_names = set(self._commands.keys())

    def clear_user_commands(self) -> None:
        """Remove all non-builtin commands and external completers."""
        self._commands = {
            k: v for k, v in self._commands.items() if k in self._builtin_names
        }
        self._external_completers.clear()


registry = CommandRegistry()
