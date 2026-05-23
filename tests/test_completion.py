import os
import tempfile

from cshell2.completion import (
    ChoiceCompleter,
    CompletionContext,
    ConditionalCompleter,
    FileCompleter,
    OptionsCompleter,
)
from cshell2.context import Context
from cshell2.shell import _positional_index


def make_ctx(prefix="", args=None, command="test"):
    return CompletionContext(
        command=command,
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line="",
        shell_context=None,
    )


def test_choice_completer():
    c = ChoiceCompleter(["alpha", "beta", "gamma"])
    results = c.complete(make_ctx(prefix="a"))
    assert len(results) == 1
    assert results[0].value == "alpha"


def test_choice_completer_empty_prefix():
    c = ChoiceCompleter(["alpha", "beta"])
    results = c.complete(make_ctx(prefix=""))
    assert len(results) == 2


def test_file_completer():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "foo.txt"), "w").close()
        open(os.path.join(tmpdir, "bar.py"), "w").close()
        os.mkdir(os.path.join(tmpdir, "subdir"))

        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            c = FileCompleter()
            results = c.complete(make_ctx(prefix="f"))
            assert any(r.value == "foo.txt" for r in results)

            results = c.complete(make_ctx(prefix=""))
            values = [r.value for r in results]
            assert "foo.txt" in values
            assert "bar.py" in values
            assert "subdir/" in values
        finally:
            os.chdir(old_cwd)


def test_conditional_completer():
    c = ConditionalCompleter({
        ("prod",): ChoiceCompleter(["us-east-1", "us-west-2"]),
        ("staging",): ChoiceCompleter(["eu-west-1"]),
    })
    results = c.complete(make_ctx(prefix="us", args=["prod"]))
    assert len(results) == 2

    results = c.complete(make_ctx(prefix="", args=["staging"]))
    assert len(results) == 1
    assert results[0].value == "eu-west-1"


def test_options_completer_shows_value_taking_flags():
    """Flags registered only in `args` (not in `options`) must still appear."""
    c = OptionsCompleter(
        {"-n": "dry run", "-v": "verbose"},
        args={
            "-t": ("SECONDS", ChoiceCompleter(["30", "60"])),
            "-b": "BRANCH",
        },
    )
    results = c.complete(make_ctx(prefix="-"))
    values = [r.value for r in results]
    # Boolean flags
    assert "-n" in values
    assert "-v" in values
    # Value-taking flags must also be present
    assert "-t" in values, "-t (args-only flag) missing from completions"
    assert "-b" in values, "-b (args-only flag) missing from completions"
    # arg_hints are set correctly
    t = next(r for r in results if r.value == "-t")
    assert t.arg_hint == "SECONDS"
    b = next(r for r in results if r.value == "-b")
    assert b.arg_hint == "BRANCH"
    # Value-taking flags are not combinable
    assert not t.combinable
    assert not b.combinable


def _make_options_completer():
    return OptionsCompleter(
        {"-n": "dry run", "-v": "verbose"},
        args={
            "-t": ("SECONDS", ChoiceCompleter(["30", "60"])),
            "-b": "BRANCH",
        },
    )


def test_positional_index_no_flags():
    assert _positional_index([], None) == 0
    assert _positional_index(["prod"], None) == 1
    assert _positional_index(["prod", "api"], None) == 2


def test_positional_index_boolean_flags_skipped():
    oc = _make_options_completer()
    # "deploy -n <TAB>" → first positional not yet given
    assert _positional_index(["-n"], oc) == 0
    # "deploy -n -v <TAB>" → still 0
    assert _positional_index(["-n", "-v"], oc) == 0
    # "deploy prod -n <TAB>" → one positional seen
    assert _positional_index(["prod", "-n"], oc) == 1
    # "deploy prod -n api <TAB>" → two positionals seen
    assert _positional_index(["prod", "-n", "api"], oc) == 2


def test_positional_index_value_taking_flags_consume_value_token():
    oc = _make_options_completer()
    # "deploy -t 60 <TAB>" → -t + 60 skipped, positional = 0
    assert _positional_index(["-t", "60"], oc) == 0
    # "deploy prod -t 60 <TAB>" → positional = 1
    assert _positional_index(["prod", "-t", "60"], oc) == 1
    # "deploy -b main prod <TAB>" → -b + main skipped, prod counted
    assert _positional_index(["-b", "main", "prod"], oc) == 1
    # mixed: "deploy -n prod -t 60 api <TAB>" → positional = 2
    assert _positional_index(["-n", "prod", "-t", "60", "api"], oc) == 2


def test_completer_receives_shell_context():
    ctx = CompletionContext(
        command="deploy",
        args=[],
        arg_index=0,
        prefix="",
        line="deploy ",
        shell_context=Context(name="prod"),
    )
    assert ctx.shell_context.name == "prod"
