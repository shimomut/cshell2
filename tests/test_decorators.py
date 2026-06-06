"""Tests for pipeline decorators (@name [flags] body).

Exercises the parser (brace handling, flag extraction, body building),
the registry, the executor dispatch, and ``@<TAB>`` completion.
"""

from __future__ import annotations

import pytest

from cshell2.commands import arg, registry as command_registry
from cshell2.decorators import (
    parse_decorator_args,
    registry as decorator_registry,
)
from cshell2.pipeline import (
    DecoratorParseError,
    Pipeline,
    Stage,
    parse_line,
    set_pipeline_executor,
)
from cshell2.shell import Shell


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_test_decorators():
    """Snapshot decorator + command registries; restore after each test."""
    deco_before = dict(decorator_registry._decorators)
    cmd_before = dict(command_registry._commands)
    yield
    # Drop only the new entries added during the test.
    for name in list(decorator_registry._decorators):
        if name not in deco_before:
            del decorator_registry._decorators[name]
    for name in list(command_registry._commands):
        if name not in cmd_before:
            del command_registry._commands[name]


@pytest.fixture
def watch_deco():
    """Register a minimal @watch-shaped decorator for parser tests."""
    @decorator_registry.decorator(
        name="watch",
        params=[
            arg("-n", "--interval", type=float, default=2.0),
            arg("--no-clear", action="store_true"),
        ],
    )
    def _watch(pipeline, *, interval, no_clear):
        pass
    return decorator_registry.get("watch")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_register_decorator(watch_deco):
    assert decorator_registry.has("watch")
    assert "watch" in decorator_registry.list_decorators()
    assert watch_deco.name == "watch"


def test_parse_decorator_args(watch_deco):
    assert parse_decorator_args(watch_deco, []) == {"interval": 2.0, "no_clear": False}
    assert parse_decorator_args(watch_deco, ["-n", "5"]) == {"interval": 5.0, "no_clear": False}
    assert parse_decorator_args(watch_deco, ["--no-clear"]) == {"interval": 2.0, "no_clear": True}
    # argparse rejects unknown flags — should return None, not raise.
    assert parse_decorator_args(watch_deco, ["--bogus"]) is None


def test_flag_takes_value(watch_deco):
    assert decorator_registry.flag_takes_value("watch", "-n") is True
    assert decorator_registry.flag_takes_value("watch", "--interval") is True
    assert decorator_registry.flag_takes_value("watch", "--no-clear") is False
    assert decorator_registry.flag_takes_value("watch", "--missing") is False
    assert decorator_registry.flag_takes_value("nonexistent", "-n") is False


def test_clear_user_decorators_keeps_builtins(watch_deco):
    decorator_registry.mark_builtins()

    @decorator_registry.decorator(name="_t_user")
    def _user(pipeline):
        pass

    assert decorator_registry.has("_t_user")
    decorator_registry.clear_user_decorators()
    assert decorator_registry.has("watch")  # builtin survives
    assert not decorator_registry.has("_t_user")  # user decorator gone


# ---------------------------------------------------------------------------
# Parser — single-command form
# ---------------------------------------------------------------------------

def _decorator_stage(line: str) -> Stage:
    """Parse *line* and return the single decorator-stage in it."""
    seq = parse_line(line)
    assert len(seq.items) == 1
    pipeline = seq.items[0][1]
    assert len(pipeline.stages) == 1
    stage = pipeline.stages[0]
    assert stage.decorator is not None
    return stage


def test_parse_bare_decorator_no_flags(watch_deco):
    stage = _decorator_stage("@watch ls")
    assert stage.decorator.name == "watch"
    assert stage.decorator.flag_tokens == []
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_decorator_with_value_flag(watch_deco):
    """`-n 1` consumes both tokens; `df -h` is the body."""
    stage = _decorator_stage("@watch -n 1 df -h")
    assert stage.decorator.flag_tokens == ["-n", "1"]
    assert [s.text for s in stage.decorator.body.stages] == ["df -h"]


def test_parse_decorator_with_boolean_flag(watch_deco):
    stage = _decorator_stage("@watch --no-clear ls")
    assert stage.decorator.flag_tokens == ["--no-clear"]
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_decorator_with_flag_value_then_boolean(watch_deco):
    stage = _decorator_stage("@watch -n 1 --no-clear ls")
    assert stage.decorator.flag_tokens == ["-n", "1", "--no-clear"]
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_decorator_eq_form(watch_deco):
    """`--interval=5` is a single token; argparse already understands it."""
    stage = _decorator_stage("@watch --interval=5 ls")
    assert stage.decorator.flag_tokens == ["--interval=5"]
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_decorator_double_dash(watch_deco):
    """`--` terminates flag parsing; -la is part of ls's args."""
    stage = _decorator_stage("@watch -- ls -la")
    assert stage.decorator.flag_tokens == []
    assert [s.text for s in stage.decorator.body.stages] == ["ls -la"]


def test_parse_body_with_dash_flags(watch_deco):
    """`@watch -n 5 ls -la` — `-la` belongs to ls, not @watch."""
    stage = _decorator_stage("@watch -n 5 ls -la")
    assert stage.decorator.flag_tokens == ["-n", "5"]
    assert [s.text for s in stage.decorator.body.stages] == ["ls -la"]


# ---------------------------------------------------------------------------
# Parser — braced form
# ---------------------------------------------------------------------------

def test_parse_braced_single_command(watch_deco):
    stage = _decorator_stage("@watch {ls}")
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_braced_multistage_pipeline(watch_deco):
    stage = _decorator_stage("@watch {df -h | grep abc}")
    assert [s.text for s in stage.decorator.body.stages] == ["df -h", "grep abc"]


def test_parse_braced_with_flag(watch_deco):
    stage = _decorator_stage("@watch -n 1 {df -h | grep abc}")
    assert stage.decorator.flag_tokens == ["-n", "1"]
    assert [s.text for s in stage.decorator.body.stages] == ["df -h", "grep abc"]


# ---------------------------------------------------------------------------
# Parser — brace handling: quotes, escapes, ${var}
# ---------------------------------------------------------------------------

def test_brace_handling_double_quoted_brace(watch_deco):
    stage = _decorator_stage('@watch {echo "}"}')
    assert [s.text for s in stage.decorator.body.stages] == ['echo "}"']


def test_brace_handling_single_quoted_brace(watch_deco):
    stage = _decorator_stage("@watch {grep '{' file}")
    assert [s.text for s in stage.decorator.body.stages] == ["grep '{' file"]


def test_brace_handling_escaped_brace(watch_deco):
    stage = _decorator_stage(r"@watch {echo \}}")
    assert [s.text for s in stage.decorator.body.stages] == [r"echo \}"]


def test_brace_handling_var_expansion(watch_deco):
    """``${VAR}`` inside a decorator body should not terminate the scope.

    ``parse_line`` runs after ``expand_vars``; for tokens that survive
    expansion (single-quoted), the parser's ``${...}`` brace-balancing
    keeps the outer scope intact.
    """
    stage = _decorator_stage("@watch {echo '${X}'}")
    assert [s.text for s in stage.decorator.body.stages] == ["echo '${X}'"]


# ---------------------------------------------------------------------------
# Parser — error cases
# ---------------------------------------------------------------------------

def test_unbraced_pipe_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="\\|"):
        parse_line("@watch ls | grep foo")


def test_unbraced_seq_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="&&"):
        parse_line("@watch ls && pwd")


def test_unmatched_brace_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="unmatched"):
        parse_line("@watch {ls")


def test_text_after_close_brace_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="text after"):
        parse_line("@watch {ls} | grep foo")


def test_empty_body_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="empty"):
        parse_line("@watch { }")


# ---------------------------------------------------------------------------
# Pipeline.run executor indirection
# ---------------------------------------------------------------------------

def test_pipeline_run_without_shell_raises():
    set_pipeline_executor(None)
    try:
        with pytest.raises(RuntimeError, match="no executor"):
            Pipeline(stages=[Stage(text="echo hi")]).run()
    finally:
        # leave the slot clean for the next test (any test instantiating
        # Shell will re-register).
        pass


def test_pipeline_run_with_shell_routes_to_executor(tmp_path):
    """Shell registers the executor on construction.  Pipeline.run() then
    delegates without raising RuntimeError.

    Use a redirect so the single-stage path runs synchronously rather than
    going through the PTY-forwarding loop (which can't open a controlling
    terminal under pytest's stdin capture).
    """
    Shell()
    captured = []

    @command_registry.command(name="_t_capture_arg")
    def _capture():
        captured.append("ran")
        print("ok")

    out = tmp_path / "out"
    from cshell2.pipeline import Redirect
    Pipeline(
        stages=[Stage(text="_t_capture_arg", redirects=[Redirect(kind=">", target=str(out))])],
    ).run()
    assert captured == ["ran"]
    assert out.read_text().strip() == "ok"


# ---------------------------------------------------------------------------
# Decorator dispatch
# ---------------------------------------------------------------------------

def test_decorator_dispatch_passes_pipeline_and_args():
    """When ``@_t_capture -n 5 echo hi`` is executed, the decorator
    function receives the parsed pipeline body and the parsed flag
    value as a kwarg.  We invoke ``_execute_decorator_stage`` directly
    so we don't pull in the PTY-forwarding path (which fails under
    pytest's stdin capture)."""
    sh = Shell()
    received = {}

    @decorator_registry.decorator(name="_t_capture", params=[arg("-n", type=int, default=1)])
    def _capture(pipeline, *, n):
        received["n"] = n
        received["body_texts"] = [s.text for s in pipeline.stages]

    seq = parse_line("@_t_capture -n 5 echo hi")
    sh._execute_decorator_stage(seq.items[0][1].stages[0])
    assert received == {"n": 5, "body_texts": ["echo hi"]}


def test_unknown_decorator_returns_127(capsys):
    """Parsing is registry-agnostic: any ``@<ident>`` produces a
    decorator stage.  Dispatch is what reports an unknown name."""
    sh = Shell()
    seq = parse_line("@_t_no_such_decorator ls")
    deco_stage = seq.items[0][1].stages[0]
    rc = sh._execute_decorator_stage(deco_stage)
    assert rc == 127
    captured = capsys.readouterr()
    assert "unknown decorator" in captured.err


def test_decorator_argparse_error_returns_2():
    sh = Shell()

    @decorator_registry.decorator(name="_t_argerr", params=[arg("-n", type=int, default=1)])
    def _h(pipeline, *, n):
        pass

    seq = parse_line("@_t_argerr --bogus ls")
    rc = sh._execute_decorator_stage(seq.items[0][1].stages[0])
    assert rc == 2


# ---------------------------------------------------------------------------
# @<TAB> completion
# ---------------------------------------------------------------------------

def test_completion_decorator_name(watch_deco):
    shell = Shell()
    completions, prefix, label = shell._get_completions("@wa")
    assert label == "decorator"
    assert any(c.value == "@watch" for c in completions)


def test_completion_decorator_flags(watch_deco):
    shell = Shell()
    completions, prefix, label = shell._get_completions("@watch -")
    assert label == "@watch option"
    flag_values = {c.value for c in completions}
    assert "-n" in flag_values
    assert "--interval" in flag_values
    assert "--no-clear" in flag_values


def test_completion_after_decorator_delegates_to_command_completion(watch_deco):
    shell = Shell()
    completions, prefix, label = shell._get_completions("@watch e")
    # ``@watch e<TAB>`` should fall through to command-name completion.
    assert label == "command"
    values = {c.value for c in completions}
    assert "exit" in values  # built-in command starts with 'e'


def test_completion_after_decorator_with_flags_delegates(watch_deco):
    shell = Shell()
    completions, prefix, label = shell._get_completions("@watch -n 1 e")
    assert label == "command"
    values = {c.value for c in completions}
    assert "exit" in values


# ---------------------------------------------------------------------------
# Shell-level error handling for malformed decorator lines
# ---------------------------------------------------------------------------

def test_shell_execute_swallows_decorator_parse_error(capsys, watch_deco):
    """A malformed decorator line should print an error and return to the
    prompt, not crash the shell with an uncaught exception."""
    sh = Shell()
    sh._execute("@watch {ls")  # unmatched brace
    err = capsys.readouterr().err
    assert "unmatched" in err


def test_shell_execute_swallows_decorator_composition_error(capsys, watch_deco):
    """Decorator composition (`@deco {...} | next`) is not yet supported,
    but the parse error must not crash the shell."""
    sh = Shell()
    sh._execute("@watch {ls} | grep foo")
    err = capsys.readouterr().err
    assert "text after" in err


# ---------------------------------------------------------------------------
# sys.stdout.isatty() inside a decorator body
# ---------------------------------------------------------------------------

def test_stdout_isatty_visible_to_decorator_body():
    """``@watch`` checks ``sys.stdout.isatty()`` to decide whether to emit
    the screen-clear ANSI escape.  The ``_StdoutProxy`` installed for a
    Python-command thread must report the real stdout's tty status, not
    the io.TextIOBase default of ``False`` — otherwise ``--no-clear``
    has no observable effect (the clear path is skipped either way).

    Verified directly against the proxy: under pytest's stdin capture
    the real stdout isn't a tty, so the proxy must report False; what
    matters is that it asks the real stream rather than returning
    False unconditionally.
    """
    import sys as _sys
    from cshell2.shell import _StdoutProxy

    proxy = _StdoutProxy(_sys.__stdout__)
    assert proxy.isatty() == _sys.__stdout__.isatty()


def test_thread_local_stdout_isatty_falls_through(monkeypatch):
    """``_ThreadLocalStdout`` must forward ``isatty()`` to either the
    real stream (no override) or the thread-local override — not return
    the io.TextIOBase default of ``False``."""
    import sys as _sys
    from cshell2.shell import _ThreadLocalStdout, _StdoutProxy

    tls = _ThreadLocalStdout(_sys.__stdout__)
    # No override: reflect the real stream.
    assert tls.isatty() == _sys.__stdout__.isatty()

    # With a _StdoutProxy override (the path a PythonCommandSlot uses),
    # isatty() should reflect the proxy, which itself reflects the real
    # stream.
    proxy = _StdoutProxy(_sys.__stdout__)
    tls.set_override(proxy)
    try:
        assert tls.isatty() == _sys.__stdout__.isatty()
    finally:
        tls.clear_override()
