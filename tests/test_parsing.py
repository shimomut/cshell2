import os

from cshell2.parsing import expand_vars, tokenize, split_for_completion


def test_expand_vars_basic():
    os.environ["CSHELL2_TEST_VAR"] = "hello"
    assert expand_vars("echo $CSHELL2_TEST_VAR") == "echo hello"
    del os.environ["CSHELL2_TEST_VAR"]


def test_expand_vars_braces():
    os.environ["CSHELL2_TEST_VAR"] = "world"
    assert expand_vars("echo ${CSHELL2_TEST_VAR}!") == "echo world!"
    del os.environ["CSHELL2_TEST_VAR"]


def test_expand_vars_unset_is_empty():
    os.environ.pop("CSHELL2_TEST_UNSET", None)
    assert expand_vars("echo $CSHELL2_TEST_UNSET") == "echo "


def test_expand_vars_single_quoted_not_expanded():
    os.environ["CSHELL2_TEST_VAR"] = "hello"
    assert expand_vars("echo '$CSHELL2_TEST_VAR'") == "echo '$CSHELL2_TEST_VAR'"
    del os.environ["CSHELL2_TEST_VAR"]


def test_expand_vars_no_vars():
    assert expand_vars("ls -la /tmp") == "ls -la /tmp"


def test_tokenize_simple():
    assert tokenize("ls -la /tmp") == ["ls", "-la", "/tmp"]


def test_tokenize_quoted():
    assert tokenize('echo "hello world"') == ["echo", "hello world"]


def test_tokenize_empty():
    assert tokenize("") == []


def test_split_for_completion_mid_word():
    tokens, prefix = split_for_completion("ls -l /tm")
    assert tokens == ["ls", "-l"]
    assert prefix == "/tm"


def test_split_for_completion_trailing_space():
    tokens, prefix = split_for_completion("ls -l ")
    assert tokens == ["ls", "-l"]
    assert prefix == ""


def test_split_for_completion_command_only():
    tokens, prefix = split_for_completion("gi")
    assert tokens == []
    assert prefix == "gi"


def test_split_for_completion_empty():
    tokens, prefix = split_for_completion("")
    assert tokens == []
    assert prefix == ""
