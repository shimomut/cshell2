"""Main shell loop — input handling, command dispatch, completion integration."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer as PTKCompleter, Completion as PTKCompletion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from .commands import CommandRegistry, registry
from .completion import (
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    Completion,
)
from .context import ContextManager
from .parsing import split_for_completion

_DEFAULT_CONFIG = """\
# cshell2 user configuration
# Define custom commands and completers here.
#
# Example:
#
# from cshell2.commands import registry
# from cshell2.completion import Completer, Completion, ChoiceCompleter
#
# @registry.command(
#     name="hello",
#     completers={0: ChoiceCompleter(["world", "there"])},
# )
# def hello(name: str = "world"):
#     print(f"Hello, {name}!")
#
# Enable completion recipes for external commands:
#
# from cshell2.recipes import enable
# enable("make")
"""


class ShellCompleter(PTKCompleter):
    """Bridges cshell2's completion engine to prompt_toolkit."""

    def __init__(self, cmd_registry: CommandRegistry, context_manager: ContextManager):
        self._registry = cmd_registry
        self._context_manager = context_manager
        self._command_completer = CommandNameCompleter(cmd_registry)
        self._file_completer = FileCompleter()

    def get_completions(self, document: Document, complete_event):
        line = document.text_before_cursor
        tokens, prefix = split_for_completion(line)

        if not tokens:
            ctx = CompletionContext(
                command=None,
                args=[],
                arg_index=0,
                prefix=prefix,
                line=line,
                shell_context=self._context_manager.current(),
            )
            completions = self._command_completer.complete(ctx)
        else:
            command_name = tokens[0]
            args = tokens[1:]
            arg_index = len(args)

            ctx = CompletionContext(
                command=command_name,
                args=args,
                arg_index=arg_index,
                prefix=prefix,
                line=line,
                shell_context=self._context_manager.current(),
            )

            cmd = self._registry.get(command_name)
            completions = []
            has_completer = False
            if cmd and arg_index in cmd.completers:
                has_completer = True
                completer = cmd.completers[arg_index]
                if completer.should_activate(ctx):
                    completions = completer.complete(ctx)
            else:
                ext = self._registry.get_external_completers(command_name)
                if ext and arg_index in ext:
                    has_completer = True
                    completer = ext[arg_index]
                    if completer.should_activate(ctx):
                        completions = completer.complete(ctx)

            if not completions and not has_completer:
                completions = self._file_completer.complete(ctx)

        for c in completions:
            display_text = c.display or c.value
            yield PTKCompletion(
                c.value,
                start_position=-len(prefix),
                display=display_text,
                display_meta=c.description,
            )


class Shell:
    def __init__(self):
        self.registry = registry
        self.context_manager = ContextManager()
        self._register_builtins()
        self.registry.mark_builtins()
        self._load_user_config()

        history_path = Path.home() / ".cshell2" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)

        self.session = PromptSession(
            completer=ShellCompleter(self.registry, self.context_manager),
            history=FileHistory(str(history_path)),
        )

    def _register_builtins(self) -> None:
        from .completion import CallbackCompleter, ChoiceCompleter, Completer, Completion

        @self.registry.command(name="cd")
        def cd(path: str = "~"):
            """Change directory."""
            target = os.path.expanduser(path)
            try:
                os.chdir(target)
            except OSError as e:
                print(f"cd: {e}")

        @self.registry.command(name="exit")
        def exit_shell():
            """Exit the shell."""
            raise SystemExit(0)

        @self.registry.command(name="reload")
        def reload_config():
            """Reload ~/.cshell2/config.py."""
            self.registry.clear_user_commands()
            self._load_user_config()
            print("Config reloaded.")

        @self.registry.command(name="help")
        def help_cmd(command_name: str = ""):
            """Show help for a command, or list all commands."""
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

        context_subcommands = ChoiceCompleter(["push", "pop", "switch", "list"])
        names_after_subcommands = {"switch"}

        class ContextNameCompleter(Completer):
            def __init__(self, cm):
                self._cm = cm

            def should_activate(self, ctx: CompletionContext) -> bool:
                return bool(ctx.args) and ctx.args[0] in names_after_subcommands

            def complete(self, ctx: CompletionContext) -> list[Completion]:
                return [
                    Completion(value=n)
                    for n in self._cm.list_contexts()
                    if n.startswith(ctx.prefix)
                ]

        @self.registry.command(
            name="context",
            completers={0: context_subcommands, 1: ContextNameCompleter(self.context_manager)},
        )
        def context_cmd(*args):
            """Manage contexts: push, pop, switch, list."""
            if not args:
                ctx = self.context_manager.current()
                if ctx:
                    print(f"Current: {ctx.name} {ctx.variables}")
                else:
                    print("No active context.")
                return

            subcmd = args[0]
            rest = args[1:]

            if subcmd == "push":
                if not rest:
                    print("Usage: context push <name> [--key value ...]")
                    return
                name = rest[0]
                variables = {}
                i = 1
                while i < len(rest):
                    if rest[i].startswith("--") and i + 1 < len(rest):
                        variables[rest[i][2:]] = rest[i + 1]
                        i += 2
                    else:
                        i += 1
                if name not in self.context_manager.contexts:
                    self.context_manager.create(name, **variables)
                self.context_manager.push(name)
                print(f"Pushed context '{name}'")

            elif subcmd == "pop":
                ctx = self.context_manager.current()
                if ctx is None:
                    print("No active context.")
                    return
                name = ctx.name
                self.context_manager.pop()
                self.context_manager.remove(name)
                prev = self.context_manager.current()
                if prev:
                    print(f"Popped '{name}', now in '{prev.name}'")
                else:
                    print(f"Popped '{name}'")

            elif subcmd == "switch":
                if not rest:
                    print("Usage: context switch <name>")
                    return
                try:
                    self.context_manager.switch(rest[0])
                except KeyError as e:
                    print(e)

            elif subcmd == "list":
                names = self.context_manager.list_contexts()
                if not names:
                    print("No contexts defined.")
                else:
                    for n in names:
                        marker = "*" if n == self.context_manager.current_name else " "
                        ctx = self.context_manager.contexts[n]
                        print(f"  {marker} {n} {ctx.variables}")

            else:
                print(f"Unknown subcommand: {subcmd}")

    def _load_user_config(self) -> None:
        config_path = Path.home() / ".cshell2" / "config.py"
        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(_DEFAULT_CONFIG)
            return

        import importlib.util
        sys.modules.pop("cshell2_user_config", None)
        spec = importlib.util.spec_from_file_location("cshell2_user_config", config_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules["cshell2_user_config"] = module
            try:
                spec.loader.exec_module(module)
            except Exception as e:
                print(f"Error loading config: {e}", file=sys.stderr)

    def _get_prompt(self) -> str:
        ctx = self.context_manager.current()
        cwd = os.path.basename(os.getcwd()) or "/"
        if ctx:
            return f"[{ctx.name}] {cwd}> "
        return f"{cwd}> "

    def _execute(self, line: str) -> None:
        tokens, _ = split_for_completion(line + " ")
        if not tokens:
            return

        command_name = tokens[0]
        args = [os.path.expanduser(a) for a in tokens[1:]]

        cmd = self.registry.get(command_name)
        if cmd:
            try:
                cmd.func(*args)
            except SystemExit:
                raise
            except TypeError as e:
                print(f"{command_name}: {e}")
            except Exception as e:
                print(f"{command_name}: error: {e}")
                traceback.print_exc()
        else:
            try:
                result = subprocess.run(
                    [command_name] + args,
                    env=os.environ,
                )
            except FileNotFoundError:
                print(f"cshell2: command not found: {command_name}")
            except KeyboardInterrupt:
                print()

    def run(self) -> None:
        print("cshell2 — type 'help' for available commands, 'exit' to quit.")
        while True:
            try:
                text = self.session.prompt(
                    self._get_prompt(),
                    multiline=True,
                    prompt_continuation="> ",
                    key_bindings=self._multiline_bindings(),
                )
                line = text.replace("\\\n", "")
                if line.strip():
                    self._execute(line.strip())
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print("\nexit")
                break
            except SystemExit:
                break

    @staticmethod
    def _multiline_bindings() -> KeyBindings:
        """Enter submits unless the line ends with backslash."""
        bindings = KeyBindings()

        @bindings.add("enter")
        def _(event):
            buf = event.current_buffer
            if buf.document.text_before_cursor.endswith("\\"):
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        return bindings
