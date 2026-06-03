"""Tests for the recipe loader (enable / add_recipe_path / recipe_search_path)."""

from __future__ import annotations

import importlib
import re
import textwrap
from pathlib import Path

import pytest

import cshell2.recipes as recipes_pkg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_search_path(monkeypatch, paths: list[Path]) -> None:
    """Replace recipe_search_path for the duration of a test."""
    monkeypatch.setattr(recipes_pkg, "recipe_search_path", list(paths))


def _write_recipe(directory: Path, name: str, side_effect_list: list) -> Path:
    """Write a minimal recipe that appends *name* to *side_effect_list* on register()."""
    path = directory / f"{name}.py"
    path.write_text(
        textwrap.dedent(f"""\
            _results = {side_effect_list!r}  # reference kept by caller

            def register():
                import cshell2.recipes._test_results as _mod
                _mod.results.append({name!r})
        """)
    )
    return path


def _make_recipe(tmp_path: Path, name: str) -> Path:
    """Write a recipe that records its name in a shared results list."""
    path = tmp_path / f"{name}.py"
    path.write_text(
        textwrap.dedent(f"""\
            def register():
                import cshell2.recipes._test_results as _mod
                _mod.results.append({name!r})
        """)
    )
    return path


# We use a tiny helper sub-module to collect side-effects across dynamically
# loaded recipe files (they can't share a list via closure easily).
@pytest.fixture(autouse=True)
def _result_module(monkeypatch):
    """Inject a fresh cshell2.recipes._test_results module for each test."""
    import types
    mod = types.ModuleType("cshell2.recipes._test_results")
    mod.results = []
    monkeypatch.setitem(
        importlib.import_module("cshell2.recipes").__dict__,
        "_test_results_mod",
        mod,
    )
    import sys
    monkeypatch.setitem(sys.modules, "cshell2.recipes._test_results", mod)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuiltinRecipes:
    def test_builtin_recipe_loads(self):
        """enable() can load a built-in recipe without error."""
        # 'ls' is a lightweight built-in recipe (no external dependencies).
        # We just want to confirm it loads; we don't care about completers here.
        recipes_pkg.enable("ls")   # should not raise


class TestUserRecipes:
    def test_user_recipe_in_default_path(self, tmp_path, monkeypatch, _result_module):
        _reset_search_path(monkeypatch, [tmp_path])
        _make_recipe(tmp_path, "my_tool")

        recipes_pkg.enable("my_tool")

        assert _result_module.results == ["my_tool"]

    def test_not_found_raises_import_error(self, tmp_path, monkeypatch):
        _reset_search_path(monkeypatch, [tmp_path])

        with pytest.raises(ImportError, match="nonexistent"):
            recipes_pkg.enable("nonexistent")

    def test_error_message_lists_searched_dirs(self, tmp_path, monkeypatch):
        _reset_search_path(monkeypatch, [tmp_path])

        with pytest.raises(ImportError, match=re.escape(str(tmp_path))):
            recipes_pkg.enable("ghost")


class TestAddRecipePath:
    def test_add_recipe_path_appends(self, monkeypatch):
        _reset_search_path(monkeypatch, [])
        p = Path("/some/dir")

        recipes_pkg.add_recipe_path(p)

        assert recipes_pkg.recipe_search_path == [p]

    def test_add_recipe_path_accepts_string(self, monkeypatch):
        _reset_search_path(monkeypatch, [])

        recipes_pkg.add_recipe_path("/some/dir")

        assert recipes_pkg.recipe_search_path == [Path("/some/dir")]

    def test_first_match_wins(self, tmp_path, monkeypatch, _result_module):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        _make_recipe(dir_a, "shared")
        _make_recipe(dir_b, "shared")

        _reset_search_path(monkeypatch, [dir_a, dir_b])
        recipes_pkg.enable("shared")

        # Only the recipe from dir_a (first in path) should have fired.
        assert _result_module.results == ["shared"]

    def test_falls_through_to_second_dir(self, tmp_path, monkeypatch, _result_module):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        # Recipe only in dir_b.
        _make_recipe(dir_b, "only_in_b")

        _reset_search_path(monkeypatch, [dir_a, dir_b])
        recipes_pkg.enable("only_in_b")

        assert _result_module.results == ["only_in_b"]

    def test_builtin_takes_priority_over_search_path(self, tmp_path, monkeypatch, _result_module):
        """A user file named 'ls' must not shadow the built-in ls recipe."""
        _make_recipe(tmp_path, "ls")
        _reset_search_path(monkeypatch, [tmp_path])

        # Should load built-in ls, not our fake one — so results stay empty.
        recipes_pkg.enable("ls")

        assert _result_module.results == []
