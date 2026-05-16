"""Main shell loop — input handling, command dispatch, completion integration."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import termios
import traceback
import tty
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer as PTKCompleter, Completion as PTKCompletion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from .commands import CommandRegistry, registry
from .completion import (
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    Completion,
)
from .context import ContextManager, ContextState
from .parsing import split_for_completion
from .process import ProcessSlot
from .prompt import get_prompt_func, set_prompt

_SWITCH_SENTINEL = "\x1d__SWITCH__"

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
#
# Customize the prompt:
#
# import os
# from cshell2 import set_prompt
#
# def my_prompt(context_manager):
#     ctx = context_manager.current()
#     prefix = f"\033[1;36m({ctx.name})\033[0m " if ctx else ""
#     cwd = os.path.basename(os.getcwd()) or "/"
#     return f"{prefix}\033[1;34m{cwd}\033[0m$ "
#
# set_prompt(my_prompt)
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
            set_prompt(None)
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

        context_subcommands = ChoiceCompleter(["push", "pop", "switch", "list", "kill"])
        names_after_subcommands = {"switch", "kill"}

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
                        state = ctx.state.name.lower()
                        state_str = f" ({state})" if state != "idle" else ""
                        print(f"  {marker} {n}{state_str} {ctx.variables}")

            elif subcmd == "kill":
                if not rest:
                    print("Usage: context kill <name>")
                    return
                target_name = rest[0]
                if target_name not in self.context_manager.contexts:
                    print(f"No context named '{target_name}'")
                    return
                target_ctx = self.context_manager.contexts[target_name]
                if target_ctx.process_slot and target_ctx.process_slot.is_alive():
                    target_ctx.process_slot.kill()
                    print(f"Sent SIGTERM to process in context '{target_name}'")
                else:
                    print(f"Context '{target_name}' has no running process.")

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

    def _get_prompt(self) -> ANSI:
        return ANSI(get_prompt_func()(self.context_manager))

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
            self._execute_external(command_name, args)

    def _execute_external(self, command_name: str, args: list[str]) -> None:
        ctx = self.context_manager.current()
        if ctx is None:
            self.context_manager.create("default")
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

        ctx.process_slot = slot
        slot.activate()
        result = self._enter_forwarding_mode(slot)
        if result == "switched":
            slot.deactivate()
            self._handle_switch()
        elif result == "exited":
            slot.deactivate()
            ctx.process_slot = None
            exit_code = slot.exit_code
            if exit_code and exit_code != 0:
                print(f"\n[Process exited with code {exit_code}]")

    def _enter_forwarding_mode(self, slot: ProcessSlot) -> str:
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

    def _show_switch_menu(self) -> str | None:
        """Show context switch menu. Returns context name to switch to, or None."""
        contexts = self.context_manager.list_contexts()
        current = self.context_manager.current_name

        print("\n--- Context Switch (Ctrl+]) ---")
        for i, name in enumerate(contexts):
            ctx = self.context_manager.contexts[name]
            state = ctx.state.name.lower()
            marker = "*" if name == current else " "
            print(f"  {marker} [{i + 1}] {name} ({state})")
        print("  [n] New context")
        print("  [q] Cancel")

        try:
            choice = input("Switch to: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if choice == "q" or not choice:
            return None
        if choice == "n":
            try:
                name = input("Context name: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if name:
                if name not in self.context_manager.contexts:
                    self.context_manager.create(name)
                return name
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(contexts):
                target = contexts[idx]
                if target == current:
                    return None
                return target
        except ValueError:
            if choice in self.context_manager.contexts:
                return choice

        print("Invalid choice.")
        return None

    def _handle_switch(self) -> None:
        """Handle Ctrl+] switch request."""
        ctx = self.context_manager.current()
        if ctx and ctx.process_slot:
            ctx.process_slot.deactivate()

        target_name = self._show_switch_menu()

        if target_name is None:
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                ctx.process_slot.activate()
            return

        self.context_manager.switch(target_name)
        new_ctx = self.context_manager.current()
        if new_ctx and new_ctx.process_slot and new_ctx.process_slot.is_alive():
            new_ctx.process_slot.activate()

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
                    ctx.process_slot.buffer.drain()  # discard stale buffer
                    # Restore terminal modes the child had active
                    restore_seq = ctx.process_slot.restore_terminal_modes()
                    if restore_seq:
                        sys.stdout.write(restore_seq)
                        sys.stdout.flush()
                    ctx.process_slot.activate()
                    # Force child to redraw
                    try:
                        rows, cols = os.get_terminal_size()
                        ctx.process_slot.resize(rows, cols)
                    except OSError:
                        pass
                    result = self._enter_forwarding_mode(ctx.process_slot)
                    ctx.process_slot.deactivate()
                    if result == "switched":
                        self._handle_switch()
                        continue
                    else:
                        exit_code = ctx.process_slot.exit_code
                        ctx.process_slot = None
                        if exit_code and exit_code != 0:
                            print(f"\n[Process exited with code {exit_code}]")
                        continue

                if ctx and ctx.process_slot and not ctx.process_slot.is_alive():
                    ctx.process_slot.replay_buffer()
                    exit_code = ctx.process_slot.exit_code
                    ctx.process_slot = None
                    if exit_code and exit_code != 0:
                        print(f"\n[Process exited with code {exit_code}]")

                text = self.session.prompt(
                    self._get_prompt(),
                    multiline=True,
                    prompt_continuation="> ",
                    key_bindings=self._key_bindings(),
                )
                if text == _SWITCH_SENTINEL:
                    self._handle_switch()
                    continue
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

    def _key_bindings(self) -> KeyBindings:
        """Key bindings: Enter for multiline, Ctrl+] for context switch."""
        bindings = KeyBindings()

        @bindings.add("enter")
        def _(event):
            buf = event.current_buffer
            if buf.document.text_before_cursor.endswith("\\"):
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        @bindings.add("c-]")
        def _switch(event):
            event.app.exit(result=_SWITCH_SENTINEL)

        return bindings

    def _install_sigwinch_handler(self) -> None:
        def on_resize(signum, frame):
            ctx = self.context_manager.current()
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                try:
                    rows, cols = os.get_terminal_size()
                    ctx.process_slot.resize(rows, cols)
                except OSError:
                    pass

        signal.signal(signal.SIGWINCH, on_resize)
