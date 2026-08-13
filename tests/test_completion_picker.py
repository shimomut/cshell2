"""TAB-completion picker behaviour: no default selection, no invisible picker.

Two rules are pinned here:

1. The completion pickers open with *nothing* highlighted, so Enter dismisses
   the list instead of inserting the first candidate.
2. A picker never stays open with zero candidates (it would render no rows
   while still eating keystrokes), and whatever the user typed while the
   picker was up is committed to the line buffer on every exit path.
"""

from cshell2.completion import Completion
from cshell2.lineedit import History, LineEditor
from cshell2.tui import InlineMultiPicker, InlinePicker


# ── InlinePicker: selection state ────────────────────────────────────────────


def test_opens_with_no_selection_when_select_first_false():
    p = InlinePicker(["a", "b", "c"], select_first=False)
    assert p._selected == -1
    assert p._current() is None


def test_opens_on_first_item_by_default():
    p = InlinePicker(["a", "b", "c"])
    assert p._selected == 0
    assert p._current() == "a"


def test_down_from_no_selection_enters_at_top():
    p = InlinePicker(["a", "b", "c"], select_first=False)
    p._move(1)
    assert p._current() == "a"
    p._move(1)
    assert p._current() == "b"


def test_up_from_no_selection_enters_at_bottom():
    p = InlinePicker(["a", "b", "c"], select_first=False)
    p._move(-1)
    assert p._current() == "c"


def test_move_on_empty_list_is_a_noop():
    p = InlinePicker([], select_first=False)
    p._move(1)
    p._move(-1)
    assert p._current() is None


def test_narrowing_resets_to_no_selection(capsys):
    items = ["alpha", "alto", "beta"]
    p = InlinePicker(
        items,
        select_first=False,
        refresh_fn=lambda typed: ([i for i in items if i.startswith(typed)], 0),
    )
    p._move(1)
    assert p._current() == "alpha"
    assert p._handle_char("a") is False   # narrows to alpha/alto
    assert p._items == ["alpha", "alto"]
    assert p._current() is None           # selection cleared, not left on alpha
    capsys.readouterr()


def test_preview_skipped_when_nothing_selected():
    calls: list[str] = []

    def preview(item):
        calls.append(item)
        return ["preview"]

    p = InlinePicker(["a", "b"], select_first=False, preview_fn=preview, preview_height=2)
    out = p._format_preview(panel_w=10)
    assert calls == []          # no item focused → preview_fn never invoked
    assert out.count("\n") == 2  # area still reserved so the footprint is stable


# ── InlinePicker: empty candidate list closes the picker ─────────────────────


def test_typing_to_zero_candidates_closes_picker(capsys):
    items = ["alpha", "beta"]
    p = InlinePicker(
        items,
        select_first=False,
        refresh_fn=lambda typed: ([i for i in items if i.startswith(typed)], 0),
    )
    assert p._handle_char("z") is True    # closes
    assert p.closed_empty is True
    assert p.reopen is False
    assert p.typed == "z"                 # caller must commit this
    capsys.readouterr()


def test_backspacing_to_zero_candidates_closes_picker(capsys):
    p = InlinePicker(["alpha"], select_first=False, refresh_fn=lambda typed: ([], 0))
    p._typed = "ab"
    assert p._handle_backspace() is True
    assert p.closed_empty is True
    assert p.apply_backspace is False     # a buffer char must NOT be deleted
    assert p.typed == "a"
    capsys.readouterr()


def test_tab_with_nothing_to_extend_selects_first_item(capsys):
    """Second TAB (no common prefix left) moves onto the first candidate."""
    items = ["alpha", "beta"]
    p = InlinePicker(
        items,
        select_first=False,
        value_fn=str,
        refresh_fn=lambda typed: (items, 0),
    )
    assert p._handle_tab_complete() is False   # no shared prefix to type
    assert p._current() is None
    # run()'s tab_complete branch then falls through to _move(1); emulate it.
    p._move(1)
    assert p._current() == "alpha"
    capsys.readouterr()


def test_enter_with_no_selection_yields_nothing():
    p = InlinePicker(["a", "b"], select_first=False)
    assert p._dispatch(b"\r") == "accept"
    assert p._current() is None           # what run() returns on accept


# ── InlineMultiPicker: same rule for the flag picker ─────────────────────────


def test_multipicker_no_default_highlight():
    p = InlineMultiPicker(["-a", "-b"], select_first=False)
    assert p._selected == -1
    p._move(1)
    assert p._selected == 0


def test_multipicker_space_with_no_highlight_checks_nothing():
    p = InlineMultiPicker(["-a", "-b"], select_first=False)
    assert p._dispatch(b" ") == "toggle"
    assert p._checked == set()


def test_multipicker_jump_works_from_no_selection():
    p = InlineMultiPicker(["-a", "-b"], select_first=False)
    p._jump_to("b")
    assert p._selected == 1


# ── LineEditor._complete: typed chars survive every exit path ────────────────


class _StubPicker:
    """Stands in for InlinePicker: replays a scripted outcome."""

    instances: list["_StubPicker"] = []
    script: list[dict] = []

    def __init__(self, items, **kwargs):
        self.items = items
        self.kwargs = kwargs
        self.reopen = False
        self.apply_backspace = False
        self.closed_empty = False
        self.action = None
        self._typed = ""
        outcome = self.script.pop(0) if self.script else {}
        self._outcome = outcome
        self._typed = outcome.get("typed", "")
        for flag in ("reopen", "apply_backspace", "closed_empty"):
            setattr(self, flag, outcome.get(flag, False))
        self._col = kwargs.get("col", 0)
        _StubPicker.instances.append(self)

    @property
    def typed(self):
        return self._typed

    def run(self):
        return self._outcome.get("selected")


def _editor(monkeypatch, tmp_path, completions_for):
    import cshell2.tui as tui

    monkeypatch.setattr(tui, "InlinePicker", _StubPicker)
    _StubPicker.instances = []
    ed = LineEditor(
        history=History(tmp_path / "history"),
        get_completions=completions_for,
        get_prompt=lambda: "> ",
    )
    monkeypatch.setattr(ed, "_redraw", lambda: None)
    return ed


def _two_candidates(line):
    return (
        [Completion(value="alpha", display="alpha"), Completion(value="beta", display="beta")],
        line.rsplit(" ", 1)[-1],
        "",
    )


def test_dismissed_picker_keeps_typed_chars(monkeypatch, tmp_path, capsys):
    """Enter/Esc with nothing selected must not erase what was typed."""
    ed = _editor(monkeypatch, tmp_path, _two_candidates)
    ed._buf, ed._cursor = "ls al", 5
    _StubPicker.script = [{"selected": None, "typed": "ph"}]

    ed._complete(0)

    assert ed._buf == "ls alph"
    assert ed._cursor == 7
    capsys.readouterr()


def test_empty_close_keeps_typed_chars(monkeypatch, tmp_path, capsys):
    ed = _editor(monkeypatch, tmp_path, _two_candidates)
    ed._buf, ed._cursor = "ls al", 5
    _StubPicker.script = [{"selected": None, "typed": "zz", "closed_empty": True}]

    ed._complete(0)

    assert ed._buf == "ls alzz"
    capsys.readouterr()


def test_accepting_a_candidate_replaces_the_whole_token(monkeypatch, tmp_path, capsys):
    """Typed chars are committed first, then the token is replaced wholesale."""
    ed = _editor(monkeypatch, tmp_path, _two_candidates)
    ed._buf, ed._cursor = "ls al", 5
    _StubPicker.script = [{"selected": Completion(value="alpha"), "typed": "p"}]

    ed._complete(0)

    assert ed._buf == "ls alpha "
    capsys.readouterr()


def test_completion_picker_opens_without_a_default_selection(monkeypatch, tmp_path, capsys):
    ed = _editor(monkeypatch, tmp_path, _two_candidates)
    ed._buf, ed._cursor = "ls al", 5
    _StubPicker.script = [{"selected": None}]

    ed._complete(0)

    assert _StubPicker.instances[0].kwargs["select_first"] is False
    capsys.readouterr()
