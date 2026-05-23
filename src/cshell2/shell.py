"""Main shell loop — input handling, command dispatch, completion integration."""

from __future__ import annotations

import os
import re
import select
import signal
import sys
import termios
import traceback
import tty
from pathlib import Path

from .commands import CommandRegistry, registry
from .completion import (
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    Completion,
)
from .variables import var_registry, VarCompleter
from .context import ContextManager, ContextState
from .lineedit import CONTEXT_CHANGED_SENTINEL, History, LineEditor, SWITCH_SENTINEL
from .parsing import expand_vars, split_for_completion, tokenize
from .pipeline import Redirect, Sequence, Stage, Pipeline, expand_globs, parse_line, _split_on_operators
from .process import ProcessSlot
from .prompt import get_prompt_func, set_prompt

_DEFAULT_CONFIG = """\
# cshell2 user configuration
# Define custom commands and completers here.
#
# ── Simple example: one positional argument ───────────────────────────────────
#
# from cshell2.commands import registry
# from cshell2.completion import ChoiceCompleter
#
# @registry.command(
#     name="hello",
#     completers={0: ChoiceCompleter(["world", "there"])},
# )
# def hello(name: str = "world"):
#     '''Greet someone by name.'''
#     print(f"Hello, {name}!")
#
#
# ── Complex example: multiple positional args, flags with/without values, ─────
# ── and a long-running task that supports Ctrl+] context switching mid-run ────
#
# Each arg() call in params= serves double duty:
#   • The kwargs are forwarded to argparse to build the parser (validation,
#     type coercion, defaults, --help text).
#   • choices= on positional args and completer= on any arg drive TAB
#     completion — no separate completers= dict is needed.
#
# The registry builds both the parser and the TAB-completion index from the
# same params list, then calls the function with typed keyword arguments.
# The function body contains zero argument-parsing code.
#
# from cshell2.commands import registry, arg
# from cshell2.completion import ChoiceCompleter  # only needed for completer=
# import time
#
# @registry.command(
#     name="deploy",
#     params=[
#         # choices= drives argparse validation AND TAB completion simultaneously.
#         arg("environment", choices=["prod", "staging", "dev"]),
#         arg("service",     nargs="?", default="all",
#                            choices=["api", "web", "worker"]),
#         # Boolean flags: help= text appears in --help output and the completion menu.
#         # Combined short flags (-nv) are expanded automatically by argparse.
#         arg("-n", "--dry-run",  action="store_true",
#                                 help="show steps, skip execution"),
#         arg("-v", "--verbose",  action="store_true",
#                                 help="print details for each step"),
#         # Value-taking flags: completer= drives TAB completion for the value.
#         arg("-t", "--timeout",  type=int, default=60,  metavar="SECONDS",
#                                 help="deployment timeout in seconds",
#                                 completer=ChoiceCompleter(["30", "60", "120", "300"])),
#         arg("-b", "--branch",   default="main",        metavar="BRANCH",
#                                 help="git branch to deploy"),
#     ],
# )
# def deploy(environment, service, dry_run, verbose, timeout, branch):
#     '''Deploy a service to an environment.
#
#     Usage: deploy <environment> [service] [-n] [-v] [-t SECONDS] [-b BRANCH]
#
#     This is a long-running command. While it executes, press Ctrl+] to open
#     the context picker and switch to (or create) another context without
#     interrupting the deployment. Switch back to this context at any time to
#     resume watching its output.
#     '''
#     import time
#
#     # All arguments arrive pre-parsed and typed — no manual parsing needed.
#     prefix = "[DRY RUN] " if dry_run else ""
#     print(f"{prefix}Deploying '{service}' to '{environment}'  "
#           f"branch={branch!r}  timeout={timeout}s")
#
#     for step, secs in [("Build image",       2),
#                        ("Push to registry",   3),
#                        ("Update deployment",  2),
#                        ("Wait for rollout",   4),
#                        ("Health checks",      2)]:
#         if verbose:
#             print(f"  -> {step} ...", flush=True)
#         if not dry_run:
#             time.sleep(secs)   # <- Ctrl+] here switches context without killing this
#         print(f"  ok {step}")
#     print(f"{prefix}Done.")
#
#
# ── Enable completion recipes for external commands ───────────────────────────
#
# from cshell2.recipes import enable
# enable("make", "git")
#
#
# ── Customize the prompt ──────────────────────────────────────────────────────
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
        if token.startswith("-"):
            i += 2 if token in value_taking else 1
        else:
            pos += 1
            i += 1
    return pos


class Shell:
    def __init__(self):
        self.registry = registry
        self.context_manager = ContextManager()
        self.context_manager.create("default")
        self._register_builtins()
        self.registry.mark_builtins()
        var_registry.mark_builtins()
        self._load_user_config()

        history_path = Path.home() / ".cshell2" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)

        history = History(history_path)
        self._line_editor = LineEditor(
            history=history,
            get_completions=self._get_completions,
            get_prompt=lambda: get_prompt_func()(self.context_manager),
            switch_fn=self._handle_switch,
        )

        self._command_completer = CommandNameCompleter(self.registry)
        self._file_completer = FileCompleter()
        self._var_completer = VarCompleter()

    def _get_completions(self, line_before_cursor: str) -> tuple[list[Completion], str]:
        # Isolate the current pipeline stage so completions for `ls | grep -`
        # are computed against `grep`, not `ls`.
        stage_line = _split_on_operators(line_before_cursor, [";", "&&", "||", "|"])[-1][1]
        tokens, prefix = split_for_completion(stage_line)

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
                return self._var_completer.complete(ctx), prefix

            ctx = CompletionContext(
                command=None,
                args=[],
                arg_index=0,
                prefix=prefix,
                line=line_before_cursor,
                shell_context=self.context_manager.current(),
            )
            return self._command_completer.complete(ctx), prefix

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
        ext = self.registry.get_external_completers(command_name)
        completers_dict = cmd.completers if cmd else ext

        has_completer = False
        completions: list[Completion] = []

        if completers_dict is not None:
            options_completer = completers_dict.get(None)
            positional_completer = completers_dict.get(
                _positional_index(args, options_completer)
            )

            # When the last arg is a value-taking flag (e.g. "du -d <TAB>"),
            # suppress positional/file completion and return a hint instead.
            # Skip when the user is already typing another flag (prefix starts
            # with "-") — they should see the options picker, not the hint.
            if (options_completer and ctx.args and not ctx.prefix.startswith("-")
                    and hasattr(options_completer, "get_preceding_flag_hint")):
                hint_info = options_completer.get_preceding_flag_hint(ctx)
                if hint_info:
                    flag, arg_hint, description, value_completer = hint_info
                    if value_completer:
                        # Flag has a dedicated value completer (e.g. -C DIR → DirCompleter).
                        return value_completer.complete(ctx), ctx.prefix
                    # No value completer: suppress file fallback and show an inline hint.
                    return [Completion(
                        value=flag,
                        display=f"<{arg_hint}>",
                        description=description,
                        arg_hint=arg_hint,
                        is_arg_hint=True,
                    )], ctx.prefix

            # Options completer takes priority when typing a "-"-prefixed token.
            if options_completer and ctx.prefix.startswith("-"):
                has_completer = True
                if options_completer.should_activate(ctx):
                    completions = options_completer.complete(ctx)

            # Positional completer as fallback (or primary when no "-" prefix).
            if not completions and positional_completer:
                has_completer = True
                if positional_completer.should_activate(ctx):
                    completions = positional_completer.complete(ctx)

        if not completions and not has_completer:
            completions = self._file_completer.complete(ctx)

        return completions, prefix

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
            running = self._running_contexts()
            if running and not self._confirm_exit(running):
                return
            raise SystemExit(0)

        @self.registry.command(name="reload")
        def reload_config():
            """Reload ~/.cshell2/config.py."""
            self.registry.clear_user_commands()
            var_registry.clear_user_vars()
            set_prompt(None)
            self._load_user_config()
            print("Config reloaded.")

        @self.registry.command(name="var", completers={0: VarCompleter()})
        def var_cmd(*args):
            """Set, unset, or list context variables.

            var                  - list all registered vars and env vars
            var NAME             - print current value of NAME
            var NAME=VALUE       - set NAME to VALUE
            var NAME=            - unset NAME (remove from env / context)

            NAME may be a registered Python-backed variable (e.g. 'aws_region')
            or a plain environment variable name.  Registered variables handle
            their own set logic (e.g. writing multiple env keys at once).
            """
            if not args:
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
            for arg in args:
                if "=" in arg:
                    key, _, value = arg.partition("=")
                    if value == "":
                        self._unset_variable(key)
                    else:
                        self._set_variable(key, value)
                elif var_registry.get(arg) is not None:
                    # 'var NAME' with no '=' → print current value of Python-backed var
                    v = var_registry.get(arg)
                    val = v.get()
                    print(f"{arg}={val}" if val is not None else f"{arg}=(unset)")
                elif arg in os.environ:
                    # 'var NAME' for a plain env var → print its value
                    print(f"{arg}={os.environ[arg]}")
                else:
                    print(f"var: invalid argument '{arg}' (expected NAME=VALUE or NAME= to unset)")

        @self.registry.command(
            name="help",
            completers={0: CallbackCompleter(lambda: sorted(self.registry.list_commands()))},
        )
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
            completers={0: context_subcommands, 1: ContextNameCompleter(self.context_manager)},
        )
        def context_cmd(*args):
            """Manage contexts: push, pop, switch, list."""
            if not args:
                ctx = self.context_manager.current()
                if ctx:
                    vars_str = f" {ctx.variables}" if ctx.variables else ""
                    print(f"Current: {ctx.name}{vars_str}")
                else:
                    print("No active context.")
                return

            subcmd = args[0]
            rest = args[1:]

            if subcmd == "push":
                if not rest:
                    print("Usage: context push <name>")
                    return
                name = rest[0]
                if name in self.context_manager.contexts:
                    print(f"Context '{name}' already exists.")
                    return
                parent = self.context_manager.current()
                inherited = dict(parent.variables) if parent else {}
                self.context_manager.create(name, variables=inherited)
                self.context_manager.push(name)
                print(f"Pushed context '{name}'")

            elif subcmd == "pop":
                ctx = self.context_manager.current()
                if ctx is None:
                    print("No active context.")
                    return
                if len(self.context_manager.list_contexts()) <= 1:
                    print("Cannot remove the last context.")
                    return
                name = ctx.name
                self.context_manager.pop()
                self.context_manager.remove(name)
                prev = self.context_manager.current()
                if prev is None:
                    remaining = self.context_manager.list_contexts()
                    if remaining:
                        self.context_manager.switch(remaining[0])
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
        """Expand variables, tokenize, glob-expand a stage's text."""
        tokens = tokenize(stage.text + " ")
        tokens = [os.path.expanduser(t) for t in tokens]
        return expand_globs(tokens)

    def _execute_pipeline(self, pipeline: Pipeline) -> int:
        """Execute a pipeline; return exit code of last stage."""
        stages = pipeline.stages
        if len(stages) == 1:
            return self._execute_stage(stages[0], stdin_fd=None, stdout_fd=None)

        # Multi-stage pipeline: connect with OS pipes.
        # All stages except the last use plain subprocess (no PTY).
        import subprocess

        n = len(stages)
        pipe_fds: list[tuple[int, int]] = []
        for _ in range(n - 1):
            pipe_fds.append(os.pipe())

        procs: list[subprocess.Popen] = []
        for idx, stage in enumerate(stages):
            tokens = self._tokenize_stage(stage)
            if not tokens:
                continue

            stdin_fd = pipe_fds[idx - 1][0] if idx > 0 else None
            stdout_fd = pipe_fds[idx][1] if idx < n - 1 else None

            stdin_src = subprocess.PIPE if stdin_fd is not None else None
            stdout_dst = subprocess.PIPE if stdout_fd is not None else None

            # Apply explicit redirects
            stdin_file = stdout_file = stderr_dst = None
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

            stdin_arg = stdin_file if stdin_file else (pipe_fds[idx - 1][0] if idx > 0 else None)
            stdout_arg = stdout_file if stdout_file else (pipe_fds[idx][1] if idx < n - 1 else None)

            try:
                p = subprocess.Popen(
                    tokens,
                    stdin=stdin_arg,
                    stdout=stdout_arg,
                    stderr=stderr_dst,
                    env=dict(os.environ),
                    cwd=os.getcwd(),
                )
            except FileNotFoundError:
                print(f"cshell2: command not found: {tokens[0]}")
                p = None
            except OSError as e:
                print(f"cshell2: {e}")
                p = None

            # Close pipe ends in parent after handing them to child
            if idx > 0:
                os.close(pipe_fds[idx - 1][0])
            if idx < n - 1:
                os.close(pipe_fds[idx][1])

            if stdin_file:
                stdin_file.close()
            if stdout_file:
                stdout_file.close()
            if stderr_dst and stderr_dst not in (subprocess.STDOUT,):
                stderr_dst.close()

            if p:
                procs.append(p)

        exit_code = 0
        for p in procs:
            p.wait()
            exit_code = p.returncode or 0
        return exit_code

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
        if cmd:
            # Python command — redirect sys.stdout/stdin if needed
            import io as _io
            old_stdout = sys.stdout
            old_stdin = sys.stdin
            old_stderr = sys.stderr
            try:
                if stdout_override:
                    sys.stdout = _io.TextIOWrapper(stdout_override)
                if stdin_override:
                    sys.stdin = _io.TextIOWrapper(stdin_override)
                if stderr_override == "stdout":
                    sys.stderr = sys.stdout
                elif stderr_override:
                    sys.stderr = _io.TextIOWrapper(stderr_override)
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

    def _execute_external(self, command_name: str, args: list[str]) -> None:
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

    def _handle_switch(self) -> bool:
        """Handle Ctrl+] switch request. Returns True if new context has a live process."""
        ctx = self.context_manager.current()
        if ctx and ctx.process_slot:
            ctx.process_slot.deactivate()

        result = self._show_switch_menu()

        if result is None:
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                ctx.process_slot.activate()
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
        if new_ctx and new_ctx.process_slot and new_ctx.process_slot.is_alive():
            new_ctx.process_slot.activate()
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
                    ctx.process_slot.buffer.drain()
                    restore_seq = ctx.process_slot.restore_terminal_modes()
                    if restore_seq:
                        sys.stdout.write(restore_seq)
                        sys.stdout.flush()
                    ctx.process_slot.activate()
                    result = self._enter_forwarding_mode(ctx.process_slot, force_redraw=True)
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

                text = self._line_editor.prompt()
                if text == SWITCH_SENTINEL:
                    self._handle_switch()
                    continue
                if text == CONTEXT_CHANGED_SENTINEL:
                    continue
                if text.strip():
                    self._execute(text.strip())
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
            answer = input("Exit anyway? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")

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
