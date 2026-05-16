from cshell2.parsing import tokenize, split_for_completion


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
