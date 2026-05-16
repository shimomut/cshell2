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

from .commands import CommandRegistry, registry
from .completion import (
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    Completion,
)
from .context import ContextManager
from .parsing import split_for_completion


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
            if cmd and arg_index in cmd.completers:
                completer = cmd.completers[arg_index]
                if completer.should_activate(ctx):
                    completions = completer.complete(ctx)

            if not completions:
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
        self._load_user_config()

        history_path = Path.home() / ".cshell2" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)

        self.session = PromptSession(
            completer=ShellCompleter(self.registry, self.context_manager),
            history=FileHistory(str(history_path)),
        )

    def _register_builtins(self) -> None:
        from .completion import ChoiceCompleter

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

        @self.registry.command(name="context")
        def context_cmd(*args):
            """Manage contexts: create, switch, push, pop, list, remove."""
            if not args:
                ctx = self.context_manager.current()
                if ctx:
                    print(f"Current: {ctx.name} {ctx.variables}")
                else:
                    print("No active context.")
                return

            subcmd = args[0]
            rest = args[1:]

            if subcmd == "create":
                if not rest:
                    print("Usage: context create <name> [--key value ...]")
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
                self.context_manager.create(name, **variables)
                print(f"Created context '{name}'")

            elif subcmd == "switch":
                if not rest:
                    print("Usage: context switch <name>")
                    return
                try:
                    self.context_manager.switch(rest[0])
                except KeyError as e:
                    print(e)

            elif subcmd == "push":
                if not rest:
                    print("Usage: context push <name>")
                    return
                try:
                    self.context_manager.push(rest[0])
                except KeyError as e:
                    print(e)

            elif subcmd == "pop":
                prev = self.context_manager.pop()
                if prev:
                    print(f"Switched back to '{prev.name}'")
                else:
                    print("Context stack empty.")

            elif subcmd == "list":
                names = self.context_manager.list_contexts()
                if not names:
                    print("No contexts defined.")
                else:
                    for n in names:
                        marker = "*" if n == self.context_manager.current_name else " "
                        ctx = self.context_manager.contexts[n]
                        print(f"  {marker} {n} {ctx.variables}")

            elif subcmd == "remove":
                if not rest:
                    print("Usage: context remove <name>")
                    return
                try:
                    self.context_manager.remove(rest[0])
                    print(f"Removed context '{rest[0]}'")
                except KeyError as e:
                    print(e)

            elif subcmd == "set":
                if len(rest) < 2:
                    print("Usage: context set <key> <value>")
                    return
                try:
                    self.context_manager.set_variable(rest[0], rest[1])
                except RuntimeError as e:
                    print(e)

            else:
                print(f"Unknown subcommand: {subcmd}")

    def _load_user_config(self) -> None:
        config_path = Path.home() / ".cshell2" / "config.py"
        if not config_path.exists():
            return

        import importlib.util
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
        args = tokens[1:]

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
                line = self.session.prompt(self._get_prompt())
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
