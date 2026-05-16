from cshell2.commands import CommandRegistry
from cshell2.completion import ChoiceCompleter


def test_register_and_get():
    reg = CommandRegistry()

    @reg.command(name="greet")
    def greet(name):
        return f"Hello, {name}"

    cmd = reg.get("greet")
    assert cmd is not None
    assert cmd.name == "greet"
    assert cmd.func("world") == "Hello, world"


def test_list_commands():
    reg = CommandRegistry()

    @reg.command(name="foo")
    def foo():
        pass

    @reg.command(name="bar")
    def bar():
        pass

    assert set(reg.list_commands()) == {"foo", "bar"}


def test_command_with_completers():
    reg = CommandRegistry()
    completer = ChoiceCompleter(["a", "b"])

    @reg.command(name="test", completers={0: completer})
    def test_cmd(arg):
        pass

    cmd = reg.get("test")
    assert 0 in cmd.completers
    assert cmd.completers[0] is completer


def test_imperative_register():
    reg = CommandRegistry()

    def my_func(x):
        """Do something."""
        return x

    reg.register(my_func, name="doit")
    cmd = reg.get("doit")
    assert cmd.name == "doit"
    assert cmd.help_text == "Do something."


def test_has():
    reg = CommandRegistry()

    @reg.command(name="exists")
    def exists():
        pass

    assert reg.has("exists")
    assert not reg.has("nope")
