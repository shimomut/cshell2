import os
import tempfile

import pytest
from cshell2.context import ContextManager


def test_create_and_current():
    cm = ContextManager()
    cm.create("prod")
    ctx = cm.current()
    assert ctx.name == "prod"


def test_switch():
    cm = ContextManager()
    cm.create("prod")
    cm.create("staging")
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
    cm.create("prod")
    cm.create("staging")
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


def test_cwd_saved_and_restored_on_switch():
    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
        real_a = os.path.realpath(dir_a)
        real_b = os.path.realpath(dir_b)

        os.chdir(real_a)
        cm = ContextManager()
        cm.create("ctx_a")  # current=ctx_a, cwd=real_a

        cm.create("ctx_b")  # ctx_b created with cwd=real_a
        cm.switch("ctx_b")  # saves ctx_a's cwd as real_a, switches to ctx_b
        os.chdir(real_b)    # now in ctx_b, move to real_b

        cm.switch("ctx_a")  # saves ctx_b's cwd as real_b, restores ctx_a -> real_a
        assert os.getcwd() == real_a

        cm.switch("ctx_b")  # saves ctx_a's cwd as real_a, restores ctx_b -> real_b
        assert os.getcwd() == real_b

    os.chdir(original_cwd)


def test_cwd_saved_and_restored_on_push_pop():
    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
        real_a = os.path.realpath(dir_a)
        real_b = os.path.realpath(dir_b)

        os.chdir(real_a)
        cm = ContextManager()
        cm.create("ctx_a")  # current=ctx_a, cwd=real_a

        cm.create("ctx_b")
        cm.switch("ctx_b")
        os.chdir(real_b)    # in ctx_b, move to real_b

        cm.switch("ctx_a")  # back to ctx_a at real_a
        assert os.getcwd() == real_a

        cm.push("ctx_b")    # push saves ctx_a, restores ctx_b -> real_b
        assert os.getcwd() == real_b

        cm.pop()            # pop saves ctx_b, restores ctx_a -> real_a
        assert os.getcwd() == real_a

    os.chdir(original_cwd)
