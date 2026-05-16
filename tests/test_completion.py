import os
import tempfile

from cshell2.completion import (
    ChoiceCompleter,
    CompletionContext,
    ConditionalCompleter,
    FileCompleter,
)
from cshell2.context import Context


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


def test_completer_receives_shell_context():
    ctx = CompletionContext(
        command="deploy",
        args=[],
        arg_index=0,
        prefix="",
        line="deploy ",
        shell_context=Context(name="prod", variables={"region": "us-east-1"}),
    )
    assert ctx.shell_context.variables["region"] == "us-east-1"
