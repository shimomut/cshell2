import pytest
from cshell2.context import ContextManager


def test_create_and_current():
    cm = ContextManager()
    cm.create("prod", account="123", region="us-east-1")
    ctx = cm.current()
    assert ctx.name == "prod"
    assert ctx.variables == {"account": "123", "region": "us-east-1"}


def test_switch():
    cm = ContextManager()
    cm.create("prod", account="123")
    cm.create("staging", account="456")
    cm.switch("staging")
    assert cm.current().name == "staging"
    cm.switch("prod")
    assert cm.current().name == "prod"


def test_switch_nonexistent():
    cm = ContextManager()
    with pytest.raises(KeyError):
        cm.switch("nope")


def test_push_pop():
    cm = ContextManager()
    cm.create("prod", account="123")
    cm.create("staging", account="456")
    cm.switch("prod")
    cm.push("staging")
    assert cm.current().name == "staging"
    cm.pop()
    assert cm.current().name == "prod"


def test_pop_empty_stack():
    cm = ContextManager()
    cm.create("prod")
    result = cm.pop()
    assert cm.current_name is None


def test_list_contexts():
    cm = ContextManager()
    cm.create("a")
    cm.create("b")
    cm.create("c")
    assert set(cm.list_contexts()) == {"a", "b", "c"}


def test_remove():
    cm = ContextManager()
    cm.create("prod")
    cm.create("staging")
    cm.switch("prod")
    cm.remove("staging")
    assert "staging" not in cm.list_contexts()


def test_remove_current():
    cm = ContextManager()
    cm.create("prod")
    cm.create("staging")
    cm.switch("prod")
    cm.push("staging")
    cm.remove("staging")
    assert cm.current_name == "prod"


def test_set_get_variable():
    cm = ContextManager()
    cm.create("prod")
    cm.set_variable("region", "us-west-2")
    assert cm.get_variable("region") == "us-west-2"
    assert cm.get_variable("nonexistent") is None
