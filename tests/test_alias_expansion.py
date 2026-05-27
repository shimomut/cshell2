"""Tests for alias expansion in command-line tokenization and completion."""

from cshell2.commands import CommandRegistry
from cshell2.completion import CommandNameCompleter, CompletionContext
from cshell2.shell import Shell


def _expand(reg: CommandRegistry, tokens: list[str]) -> list[str]:
    """Run Shell._expand_alias against *reg*'s alias table without instantiating Shell."""
    # Bound-method call without a real Shell — _expand_alias only touches self.registry
    class _Stub:
        registry = reg
    return Shell._expand_alias(_Stub(), tokens)


def test_first_token_alias_replaced_with_expansion_tokens():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert _expand(reg, ["hp", "create", "foo"]) == ["awsut", "hyperpod", "create", "foo"]


def test_alias_with_no_args():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert _expand(reg, ["hp"]) == ["awsut", "hyperpod"]


def test_unaliased_first_token_passes_through():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert _expand(reg, ["ls", "-la"]) == ["ls", "-la"]


def test_alias_does_not_chain():
    """If 'hp' expands to 'awsut hyperpod' and 'awsut' itself is also an alias,
    the second alias must NOT be applied — preventing cycles."""
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    reg.alias("awsut", "should_not_expand")
    assert _expand(reg, ["hp", "create"]) == ["awsut", "hyperpod", "create"]


def test_alias_expansion_with_flags():
    reg = CommandRegistry()
    reg.alias("la", "ls -la")
    assert _expand(reg, ["la", "/tmp"]) == ["ls", "-la", "/tmp"]


def test_alias_only_replaces_first_token():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    # 'hp' as the second token must NOT expand — only the command position does.
    assert _expand(reg, ["echo", "hp", "create"]) == ["echo", "hp", "create"]


def test_empty_token_list_returned_unchanged():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert _expand(reg, []) == []


def test_alias_listed_in_command_name_completion():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    completer = CommandNameCompleter(reg)
    ctx = CompletionContext(command=None, args=[], arg_index=0, prefix="hp",
                            line="hp", shell_context=None)
    results = completer.complete(ctx)
    aliases = [r for r in results if r.value == "hp"]
    assert len(aliases) == 1
    assert "alias" in aliases[0].description
    assert "awsut hyperpod" in aliases[0].description
