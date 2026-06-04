"""Completion engine — Completer ABC, CompletionContext, built-in completers."""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .context import Context


def _to_slash(path: str) -> str:
    """Normalize OS path separators to ``/``.

    The shell uses ``/`` as the canonical separator on every platform (Windows
    file APIs accept it), keeping ``\\`` free for POSIX escaping.  ``os.path``
    helpers emit native ``\\`` on Windows, so completer output is run through
    this.  No-op on POSIX (``os.altsep`` is None there).
    """
    return path.replace(os.sep, "/") if os.altsep else path


@dataclass
class CompletionContext:
    command: str | None
    args: list[str]
    arg_index: int
    prefix: str
    line: str
    shell_context: Context | None = None


@dataclass
class Completion:
    value: str
    display: str = ""
    description: str = ""
    multi_select: bool = False
    combinable: bool = False  # True for single-char flags that can be merged (-a -l → -al)
    arg_hint: str = ""        # non-empty when the flag requires a following argument (e.g. "N")
    is_arg_hint: bool = False  # True when this completion IS the hint for a preceding flag's value

    def __post_init__(self):
        if not self.display:
            self.display = self.value


class Completer(ABC):
    @abstractmethod
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        ...

    def should_activate(self, ctx: CompletionContext) -> bool:
        return True


class DirCompleter(Completer):
    """Completes directory paths only (no files)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if prefix:
            expanded = os.path.expanduser(prefix)
            directory = os.path.dirname(expanded) or "."
            partial = os.path.basename(expanded)
        else:
            directory = "."
            partial = ""
        try:
            entries = os.listdir(directory)
        except OSError:
            return []
        result = []
        for entry in sorted(entries):
            if entry.startswith(".") and not partial.startswith("."):
                continue
            if entry.lower().startswith(partial.lower()):
                full_path = os.path.join(directory, entry)
                if os.path.isdir(full_path):
                    display_path = (
                        os.path.join(os.path.dirname(prefix), entry)
                        if prefix and os.path.dirname(prefix)
                        else entry
                    )
                    result.append(
                        Completion(value=_to_slash(display_path) + "/", display=entry + "/")
                    )
        return result


class FileCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if prefix:
            expanded_prefix = os.path.expanduser(prefix)
            directory = os.path.dirname(expanded_prefix) or "."
            partial = os.path.basename(expanded_prefix)
        else:
            directory = "."
            partial = ""

        try:
            entries = os.listdir(directory)
        except OSError:
            return []

        dirs = []
        files = []
        for entry in sorted(entries):
            if entry.startswith(".") and not partial.startswith("."):
                continue
            if entry.lower().startswith(partial.lower()):
                full_path = os.path.join(directory, entry)
                display_path = os.path.join(os.path.dirname(prefix), entry) if prefix and os.path.dirname(prefix) else entry
                display_path = _to_slash(display_path)
                if os.path.isdir(full_path):
                    dirs.append(Completion(value=display_path + "/", display=entry + "/"))
                else:
                    files.append(Completion(value=display_path, display=entry))
        return dirs + files


class CommandNameCompleter(Completer):
    def __init__(self, registry):
        self._registry = registry

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        results = []
        seen: set[str] = set()

        for name in sorted(self._registry.list_commands()):
            if name.startswith(prefix):
                results.append(Completion(value=name, description="command"))
                seen.add(name)

        if hasattr(self._registry, "list_aliases"):
            for name, expansion in sorted(self._registry.list_aliases().items()):
                if name.startswith(prefix) and name not in seen:
                    results.append(Completion(
                        value=name, description=f"alias → {expansion}"
                    ))
                    seen.add(name)

        for cmd in self._find_system_commands(prefix):
            if cmd in seen:
                continue
            results.append(Completion(value=cmd, description="system"))

        return results

    def _find_system_commands(self, prefix: str) -> list[str]:
        if not prefix:
            return []
        seen = set()
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        for d in path_dirs:
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for entry in entries:
                if entry.startswith(prefix) and entry not in seen:
                    full = os.path.join(d, entry)
                    if os.access(full, os.X_OK):
                        seen.add(entry)
        return sorted(seen)


class ChoiceCompleter(Completer):
    def __init__(self, choices: list[str]):
        self.choices = choices

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=c)
            for c in self.choices
            if c.startswith(ctx.prefix)
        ]


class CallbackCompleter(Completer):
    """Completer that calls a function to get the current list of choices."""

    def __init__(self, func):
        self.func = func

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=c)
            for c in self.func()
            if c.startswith(ctx.prefix)
        ]


class OptionsCompleter(Completer):
    """Completer for command-line flags with multi-select TUI support.

    Auto-built from the ``-``-prefixed entries of a command's ``params``
    list, so most recipes never construct one directly.  Pass an instance
    via the ``options_completer=`` kwarg on :meth:`registry.command` when a
    custom subclass is needed (e.g. tar's bundle-letter handling).
    """

    def __init__(
        self,
        options: dict[str, str],
        args: dict[str, str | tuple[str, Completer]] | None = None,
    ):
        self.options = options
        # args values may be a plain hint string ("N") or a (hint, value_completer)
        # tuple when a specific completer should be used for that flag's value.
        self.args: dict[str, str] = {}
        self._value_completers: dict[str, Completer] = {}
        for flag, spec in (args or {}).items():
            if isinstance(spec, tuple):
                hint, vc = spec
                self.args[flag] = hint
                self._value_completers[flag] = vc
            else:
                self.args[flag] = spec

    def should_activate(self, ctx: CompletionContext) -> bool:
        return ctx.prefix.startswith("-")

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        used = self._used_flags(ctx)
        result = []
        # Iterate the union of options and args so that value-taking flags
        # registered only in `args` (without a description in `options`) are
        # still shown as completions.
        all_flags = sorted(set(self.options) | set(self.args))
        for flag in all_flags:
            if not flag.startswith(prefix):
                continue
            if flag in used:
                continue
            desc = self.options.get(flag, "")
            arg_hint = self.args.get(flag, "")
            result.append(Completion(
                value=flag,
                description=desc,
                multi_select=True,
                combinable=(len(flag) == 2 and flag.startswith("-") and not arg_hint),
                arg_hint=arg_hint,
            ))
        return result

    def get_preceding_flag_hint(
        self, ctx: CompletionContext
    ) -> tuple[str, str, str, Completer | None] | None:
        """Return (flag, hint, description, value_completer) if the last completed arg is a value-taking flag.

        ``value_completer`` is a :class:`Completer` when the flag has a registered
        value completer (e.g. ``"-C": ("DIR", DirCompleter())``), otherwise ``None``.
        Returns ``None`` entirely when the preceding arg is not a known value-taking flag.
        """
        if not ctx.args:
            return None
        last_arg = ctx.args[-1]
        if not last_arg.startswith("-"):
            return None
        hint = self.args.get(last_arg)
        if not hint:
            return None
        description = self.options.get(last_arg, "")
        value_completer = self._value_completers.get(last_arg)
        return (last_arg, hint, description, value_completer)

    def _used_flags(self, ctx: CompletionContext) -> set[str]:
        """Return the set of option flags already present in ctx.args."""
        used: set[str] = set()
        for arg in ctx.args:
            if not arg.startswith("-"):
                continue
            if arg.startswith("--"):
                used.add(arg)
            else:
                # Split short-flag clusters: -hs → {-h, -s}
                for ch in arg[1:]:
                    used.add(f"-{ch}")
        return used


class ConditionalCompleter(Completer):
    """Picks a sub-completer based on preceding args."""

    def __init__(self, mapping: dict[tuple, Completer]):
        self.mapping = mapping

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        key = tuple(ctx.args)
        completer = self.mapping.get(key)
        if completer:
            return completer.complete(ctx)
        for length in range(len(ctx.args), 0, -1):
            partial_key = tuple(ctx.args[:length])
            if partial_key in self.mapping:
                return self.mapping[partial_key].complete(ctx)
        return []


# ---------------------------------------------------------------------------
# Cobra-protocol fallback
# ---------------------------------------------------------------------------
#
# Most modern Go CLIs (kubectl, helm, gh, argocd, k9s, doctl, linkerd, …) are
# built on the spf13/cobra framework, which exposes a hidden ``__complete``
# subcommand.  When a tool registers shell completions, cobra inserts a
# function that re-invokes the tool itself like::
#
#     $ kubectl __complete get po ""
#     pod         retrieve a list of pods
#     pods        (alias)
#     poddisruptionbudget
#     poddisruptionbudgets
#     :4          ← directive byte (4 = nospace, 2 = nofiles, …)
#
# Lines before the trailing ``:N`` are candidates; each line is
# ``name\tdescription`` (description optional).  This module drives that
# protocol directly — no bash, no bash-completion script needed.


# Sentinel returned by the probe to indicate "not a cobra command".
_NOT_COBRA = object()


class CobraCompleter(Completer):
    """Fallback completer that calls a tool's hidden ``__complete`` subcommand.

    Cobra-based CLIs (kubectl, helm, gh, argocd, k9s, doctl, …) ship a
    completion function that's just a wrapper around ``<cmd> __complete``.
    Calling that subcommand directly skips bash entirely, returns richer
    data (descriptions per candidate), and works on any host that has the
    tool itself installed.

    Per-command detection: on first encounter of a command, we run
    ``<cmd> __complete --help`` once and check whether the response looks
    like a cobra completion handler.  Result is cached for the rest of the
    shell session.
    """

    def __init__(self, *, timeout: float = 1.5) -> None:
        self._timeout = timeout
        # Per-command probe cache: command name → bool.
        # Missing entry means "not yet probed".
        self._is_cobra: dict[str, bool] = {}
        # Per-line completion cache: line → list[(value, description)].
        self._results: dict[str, list[tuple[str, str]]] = {}

    def should_activate(self, ctx: CompletionContext) -> bool:
        if not ctx.command:
            return False
        # Only activate for commands resolvable on PATH — avoids spawning a
        # subprocess for typos / unknown words.
        if shutil.which(ctx.command) is None:
            return False
        return self._is_cobra_command(ctx.command)

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.command or not self._is_cobra_command(ctx.command):
            return []
        line = ctx.line
        if line in self._results:
            results = self._results[line]
        else:
            results = self._invoke(ctx.command, ctx.args, ctx.prefix)
            self._results[line] = results
        prefix = ctx.prefix
        return [
            Completion(value=v, description=d)
            for v, d in results
            if v.startswith(prefix)
        ]

    # ── detection ────────────────────────────────────────────────────────

    def _is_cobra_command(self, command: str) -> bool:
        """Return True if *command* responds to ``__complete --help`` like cobra.

        Probes once per command per shell session; result is cached.
        """
        if command in self._is_cobra:
            return self._is_cobra[command]
        result = self._probe(command)
        self._is_cobra[command] = result
        return result

    def _probe(self, command: str) -> bool:
        """One-shot probe: does *command* speak the cobra protocol?"""
        try:
            proc = subprocess.run(
                [command, "__complete", "--help"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        # Cobra's __complete help text contains a recognizable phrase.  Both
        # stdout and stderr are checked because cobra writes to stdout but
        # other tools may surface our probe via stderr.
        blob = (proc.stdout or "") + (proc.stderr or "")
        if "shell completion" in blob.lower() or "ShellCompDirective" in blob:
            return True
        # Heuristic fallback: cobra always exits 0 on `__complete --help` and
        # mentions "__complete" itself in the usage line.  Many non-cobra
        # tools either error out or emit completely unrelated help text.
        if proc.returncode == 0 and "__complete" in blob:
            return True
        return False

    # ── invocation ───────────────────────────────────────────────────────

    def _invoke(
        self, command: str, args: list[str], prefix: str
    ) -> list[tuple[str, str]]:
        """Run ``<cmd> __complete <args> <prefix>``; return [(value, desc), …]."""
        argv = [command, "__complete", *args, prefix]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        # Cobra exits 0 on success; some tools may return non-zero when no
        # candidates apply.  Treat non-zero as empty.
        if proc.returncode != 0:
            return []
        return _parse_cobra_output(proc.stdout)


def _parse_cobra_output(stdout: str) -> list[tuple[str, str]]:
    """Parse cobra ``__complete`` stdout into (value, description) pairs.

    Format::

        name\tdescription
        name              (description optional)
        :N                ← trailing directive byte; ignored
        Completion ended ← optional trailing trace line; ignored

    Blank lines are dropped.
    """
    results: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        if not line:
            continue
        # Trailing directive byte — always last non-blank line.
        if line.startswith(":") and line[1:].isdigit():
            continue
        # Some cobra builds append a "Completion ended with directive: …" line.
        if line.startswith("Completion ended"):
            continue
        if "\t" in line:
            value, _, desc = line.partition("\t")
        else:
            value, desc = line, ""
        results.append((value, desc))
    return results


# Module-level singleton + enable/disable API.  Default: enabled.

_cobra_fallback: CobraCompleter | None = None
_cobra_enabled: bool = True


def enable_cobra_fallback(*, timeout: float = 1.5) -> CobraCompleter:
    """Enable the cobra-protocol fallback.

    Returns the configured :class:`CobraCompleter`.  The default state is
    *enabled* — call this only to override the timeout.
    """
    global _cobra_fallback, _cobra_enabled
    _cobra_fallback = CobraCompleter(timeout=timeout)
    _cobra_enabled = True
    return _cobra_fallback


def disable_cobra_fallback() -> None:
    """Disable the cobra-protocol fallback for this session."""
    global _cobra_enabled
    _cobra_enabled = False


def get_cobra_fallback() -> CobraCompleter | None:
    """Return the active cobra fallback, or ``None`` if disabled.

    Lazily initialises on first call.
    """
    global _cobra_fallback
    if not _cobra_enabled:
        return None
    if _cobra_fallback is None:
        _cobra_fallback = CobraCompleter()
    return _cobra_fallback


# ---------------------------------------------------------------------------
# argcomplete fallback
# ---------------------------------------------------------------------------
#
# argcomplete (https://kislyuk.github.io/argcomplete/) is the de-facto
# completion library for Python CLIs.  Tools that opt in include pipx, conda,
# pre-commit, tox, pdm, httpie, nox, virtualenv, plus many internal Amazon
# Python tools.
#
# Protocol::
#
#     _ARGCOMPLETE=1                # enable completion mode
#     _ARGCOMPLETE_IFS=$'\v'        # candidate separator (vertical tab)
#     COMP_LINE="<full line>"
#     COMP_POINT="<cursor pos>"
#     <tool>                         # write candidates to fd 8
#
# fd 8 receives the candidate list joined by ``_ARGCOMPLETE_IFS``.
#
# Detection MUST be done before invocation because non-argcomplete tools
# silently ignore the env vars and run normally — invoking ``rm`` or any
# other side-effecting binary in completion mode would actually run it.
# We detect by inspecting the executable: it must be a Python script (shim
# for a setuptools console_script, or a plain script with the marker), and
# the imported module's first 1024 bytes must contain ``PYTHON_ARGCOMPLETE_OK``.


# Script that runs the marker check using the target tool's own Python
# interpreter — which gives us the right sys.path for finding the imported
# module without executing the user's package code.
_ARGCOMPLETE_PROBE_SCRIPT = r"""
import importlib.util, sys
mod = sys.argv[1]
spec = importlib.util.find_spec(mod)
if spec is None or spec.origin is None:
    sys.exit(2)
try:
    with open(spec.origin) as f:
        head = f.read(1024)
except OSError:
    sys.exit(3)
sys.exit(0 if "PYTHON_ARGCOMPLETE_OK" in head else 1)
"""

import re as _re

# Setuptools console_script shim:
#     #!/path/to/python
#     ...
#     from <module> import <func>
#     ...
#     sys.exit(<func>())
_SHIM_IMPORT_RE = _re.compile(r"^from\s+([\w\.]+)\s+import\s+\w+\s*$", _re.MULTILINE)


class ArgcompleteCompleter(Completer):
    """Fallback completer that drives argcomplete-aware Python CLIs.

    Detection per command (cached):
      1. The executable on PATH must be readable.
      2. Either the file itself contains ``PYTHON_ARGCOMPLETE_OK`` in its
         first 1024 bytes, OR it's a setuptools console_script shim and
         the imported module's first 1024 bytes contain the marker.

    On a hit, completion runs ``<tool>`` in argcomplete mode with fd 8
    captured; candidates come back joined by ``_ARGCOMPLETE_IFS``.

    Returns ``Completion(value=..., description="")`` — argcomplete does
    support descriptions but only with a separate, less stable wire
    format; we ignore those for now.
    """

    _IFS = "\v"

    def __init__(self, *, timeout: float = 2.0) -> None:
        self._timeout = timeout
        # Per-command probe cache: command name → bool.
        self._is_argcomplete: dict[str, bool] = {}
        # Per-line completion cache: line → list[str].
        self._results: dict[str, list[str]] = {}

    def should_activate(self, ctx: CompletionContext) -> bool:
        if not ctx.command:
            return False
        if shutil.which(ctx.command) is None:
            return False
        return self._is_argcomplete_command(ctx.command)

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.command or not self._is_argcomplete_command(ctx.command):
            return []
        line = ctx.line
        if line in self._results:
            words = self._results[line]
        else:
            words = self._invoke(ctx.command, line)
            self._results[line] = words
        prefix = ctx.prefix
        return [Completion(value=w) for w in words if w.startswith(prefix)]

    # ── detection ────────────────────────────────────────────────────────

    def _is_argcomplete_command(self, command: str) -> bool:
        if command in self._is_argcomplete:
            return self._is_argcomplete[command]
        result = self._probe(command)
        self._is_argcomplete[command] = result
        return result

    def _probe(self, command: str) -> bool:
        """Inspect the executable for the argcomplete marker."""
        path = shutil.which(command)
        if path is None:
            return False
        try:
            # Read as bytes — many executables on PATH are binaries (ls,
            # grep, …) and decoding them as text would raise.  We only need
            # to find an ASCII marker so bytes-level scanning is fine.
            with open(path, "rb") as f:
                head_bytes = f.read(2048)
        except OSError:
            return False
        # If the file isn't text-decodable as UTF-8, it can't be a Python
        # script (or its shim) — it's a compiled binary.
        try:
            head = head_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return False

        # Marker present directly in the script (plain Python script that
        # opted in via ``# PYTHON_ARGCOMPLETE_OK``).
        if "PYTHON_ARGCOMPLETE_OK" in head:
            return True

        # Setuptools console_script shim — locate the imported module and
        # check it.  Bail if the shebang doesn't point at a python interpreter.
        first_line = head.split("\n", 1)[0]
        if not first_line.startswith("#!") or "py" not in first_line.lower():
            return False
        python_path = first_line[2:].strip().split()[0]
        if not os.path.isfile(python_path):
            return False

        match = _SHIM_IMPORT_RE.search(head)
        if not match:
            return False
        module = match.group(1)

        try:
            proc = subprocess.run(
                [python_path, "-c", _ARGCOMPLETE_PROBE_SCRIPT, module],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return proc.returncode == 0

    # ── invocation ───────────────────────────────────────────────────────

    def _invoke(self, command: str, line: str) -> list[str]:
        """Run *command* in argcomplete mode, capture candidates from fd 8.

        argcomplete writes candidates to fd 8 specifically (debug output
        goes to fd 9).  We allocate a pipe, move its write end onto parent
        fd 8 via ``dup2``, then pass fd 8 to the child via ``pass_fds``.
        The child inherits fd 8 pointing to the pipe; argcomplete writes
        candidates there; we read them back from the pipe's read end.
        """
        env = dict(os.environ)
        env["_ARGCOMPLETE"] = "1"
        env["_ARGCOMPLETE_IFS"] = self._IFS
        env["_ARGCOMPLETE_SHELL"] = "bash"
        env["_ARGCOMPLETE_SUPPRESS_SPACE"] = "1"
        env["COMP_LINE"] = line
        env["COMP_POINT"] = str(len(line.encode("utf-8")))
        env["COMP_TYPE"] = "9"  # 9 = TAB

        try:
            r_fd, w_fd = os.pipe()
        except OSError:
            return []

        # Snapshot whatever was on parent fd 8 before we clobber it (rare
        # but possible).  We restore it after Popen returns.
        saved_fd8 = -1
        try:
            saved_fd8 = os.dup(8)
        except OSError:
            saved_fd8 = -1  # fd 8 wasn't open; nothing to restore

        try:
            os.dup2(w_fd, 8)
            os.close(w_fd)
            w_fd = -1
            try:
                proc = subprocess.Popen(
                    [command],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(8,),
                )
            except (OSError, ValueError):
                os.close(r_fd)
                return []
        finally:
            # Restore parent fd 8 (or close it if it wasn't open before).
            if saved_fd8 >= 0:
                try:
                    os.dup2(saved_fd8, 8)
                    os.close(saved_fd8)
                except OSError:
                    pass
            else:
                try:
                    os.close(8)
                except OSError:
                    pass

        chunks: list[bytes] = []
        try:
            proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
            os.close(r_fd)
            return []

        while True:
            try:
                chunk = os.read(r_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r_fd)

        if proc.returncode != 0:
            return []
        raw = b"".join(chunks).decode("utf-8", errors="replace")
        # argcomplete joins candidates with _IFS; trailing IFS is normal.
        words = [w for w in raw.split(self._IFS) if w]
        return words


# Module-level singleton + enable/disable API.

_argcomplete_fallback: ArgcompleteCompleter | None = None
_argcomplete_enabled: bool = True


def enable_argcomplete_fallback(*, timeout: float = 2.0) -> ArgcompleteCompleter:
    """Enable the argcomplete fallback.

    Returns the configured :class:`ArgcompleteCompleter`.  The default
    state is *enabled* — call this only to override the timeout.
    """
    global _argcomplete_fallback, _argcomplete_enabled
    _argcomplete_fallback = ArgcompleteCompleter(timeout=timeout)
    _argcomplete_enabled = True
    return _argcomplete_fallback


def disable_argcomplete_fallback() -> None:
    """Disable the argcomplete fallback for this session."""
    global _argcomplete_enabled
    _argcomplete_enabled = False


def get_argcomplete_fallback() -> ArgcompleteCompleter | None:
    """Return the active argcomplete fallback, or ``None`` if disabled."""
    global _argcomplete_fallback
    if not _argcomplete_enabled:
        return None
    if _argcomplete_fallback is None:
        _argcomplete_fallback = ArgcompleteCompleter()
    return _argcomplete_fallback
