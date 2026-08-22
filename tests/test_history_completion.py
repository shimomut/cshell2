"""Tests for command-history candidates in TAB completion.

Covers the three layers the feature spans: the completer itself
(:class:`HistoryCompleter`), the shell-side merge into ``_get_completions``,
and the line editor's verbatim insertion at the completion anchor.
"""

from __future__ import annotations

import pytest

from cshell2.commands import arg, registry as command_registry
from cshell2.completion import Completion, CompletionContext, HistoryCompleter
from cshell2.lineedit import History, LineEditor, _picker_col_offset
from cshell2.shell import Shell
from cshell2.tui import InlinePicker


def _ctx(line, prefix=""):
    return CompletionContext(
        command=None, args=[], arg_index=0, prefix=prefix, line=line, shell_context=None
    )


# ---------------------------------------------------------------------------
# HistoryCompleter
# ---------------------------------------------------------------------------

def test_offers_matching_lines_most_recent_first():
    c = HistoryCompleter(lambda: ["git commit -m one", "ls -l", "git commit -m two"])
    results = c.complete(_ctx("git commit "))
    # Values start at the anchor — the picker shows what would be *added*, not
    # the "git commit " the user is already looking at.
    assert [r.value for r in results] == ["-m two", "-m one"]
    assert all(r.verbatim for r in results)
    assert all(r.description == "history" for r in results)


def test_value_starts_at_the_anchor_mid_token():
    """A partially typed token is part of the candidate, as for any completer."""
    c = HistoryCompleter(lambda: ["git commit -m one"])
    results = c.complete(_ctx("git com", prefix="com"))
    assert [r.value for r in results] == ["commit -m one"]


def test_value_starts_at_the_raw_anchor_of_a_quoted_token():
    """The anchor counts the quote character, unlike the shlex-stripped prefix."""
    c = HistoryCompleter(lambda: ["cat 'My Documents/notes.txt'"])
    results = c.complete(_ctx("cat 'My Do", prefix="My Do"))
    assert [r.value for r in results] == ["'My Documents/notes.txt'"]


def test_matches_the_whole_line_not_just_the_current_token():
    """A history entry must extend everything typed, pipes included."""
    c = HistoryCompleter(lambda: ["ls | grep foo", "cat foo"])
    results = c.complete(_ctx("ls | grep fo", prefix="fo"))
    assert [r.value for r in results] == ["foo"]


def test_deduplicates_repeated_entries():
    c = HistoryCompleter(lambda: ["make test", "make test", "make test"])
    assert len(c.complete(_ctx("make"))) == 1


def test_respects_the_limit():
    entries = [f"make target{i}" for i in range(20)]
    c = HistoryCompleter(lambda: entries, limit=3)
    results = c.complete(_ctx("make"))
    assert len(results) == 3
    # Most recent first, so the cap keeps the newest entries.
    assert [r.value for r in results] == ["make target19", "make target18", "make target17"]


@pytest.mark.parametrize("line", ["", "   "])
def test_silent_on_an_empty_line(line):
    """Bare TAB should list commands, not the last N command lines."""
    c = HistoryCompleter(lambda: ["make test", "ls -l"])
    assert c.complete(_ctx(line)) == []
    assert c.should_activate(_ctx(line)) is False


@pytest.mark.parametrize("entry", ["make test", "make test   "])
def test_skips_entries_with_nothing_to_add(entry):
    """An exact match (or one differing only by trailing space) adds no value."""
    c = HistoryCompleter(lambda: [entry])
    assert c.complete(_ctx("make test")) == []


def test_skips_entries_containing_a_newline():
    """The value is inserted verbatim, so a newline would submit on insert."""
    c = HistoryCompleter(lambda: ["make test\nrm -rf /"])
    assert c.complete(_ctx("make")) == []


# ---------------------------------------------------------------------------
# Shell._get_completions — merge behaviour
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_test_commands():
    """Drop commands registered during a test; keep the pre-existing ones."""
    before = dict(command_registry._commands)
    yield
    for name in list(command_registry._commands):
        if name not in before:
            del command_registry._commands[name]


@pytest.fixture
def shell():
    """A Shell whose context history is fully under the test's control.

    ``Shell()`` seeds the default context from the real on-disk history, which
    would make prefix matching depend on whoever ran the suite.
    """
    sh = Shell()
    sh.context_manager.current().history = []
    return sh


def _set_history(sh, entries):
    sh.context_manager.current().history = list(entries)


def test_history_candidates_come_first_and_are_labelled(shell):
    # A prefix no file and no completer can match, so history is all there is —
    # otherwise the file-completer fallback would also contribute candidates.
    _set_history(shell, ["_t_nosuchcmd _t_zzz_no_such_path --flag value"])
    completions, prefix, label = shell._get_completions("_t_nosuchcmd _t_zzz_no_such_path")
    assert [c.value for c in completions] == ["_t_zzz_no_such_path --flag value"]
    assert completions[0].verbatim
    # Nothing else matched, so the status bar names the source.
    assert label == "history"


def test_history_precedes_but_keeps_command_candidates(shell):
    _set_history(shell, ["exit 0"])
    completions, prefix, label = shell._get_completions("ex")
    assert completions[0].value == "exit 0"
    assert completions[0].verbatim
    # The command-name candidates are still all there, after the history rows.
    rest = [c for c in completions[1:]]
    assert any(c.value == "exit" and not c.verbatim for c in rest)
    assert label == "command"


def test_history_is_kept_out_of_the_flag_picker(shell):
    """Mixing a history candidate in would demote the checkbox picker to a list."""
    command_registry.command(
        "_t_flags",
        help="test command",
        params=[arg("-v", "--verbose", action="store_true", help="verbose")],
    )
    _set_history(shell, ["_t_flags -v extra"])
    completions, _, _ = shell._get_completions("_t_flags -")
    assert completions
    assert all(c.multi_select for c in completions)
    assert not any(c.verbatim for c in completions)


def test_history_is_kept_out_of_the_arg_hint(shell):
    """A lone is_arg_hint renders as a hint line, not a picker — keep it lone."""
    command_registry.command(
        "_t_hint",
        help="test command",
        params=[arg("-p", "--port", type=int, metavar="PORT", help="port number")],
    )
    _set_history(shell, ["_t_hint -p 8080"])
    completions, _, _ = shell._get_completions("_t_hint -p ")
    assert len(completions) == 1
    assert completions[0].is_arg_hint
    assert not completions[0].verbatim


# ---------------------------------------------------------------------------
# LineEditor — inserting a verbatim (multi-token) candidate
# ---------------------------------------------------------------------------

class _StubPicker:
    """Stands in for InlinePicker: records construction, replays an outcome."""

    instances: list["_StubPicker"] = []
    script: list[dict] = []

    def __init__(self, items, **kwargs):
        self.items = items
        self.kwargs = kwargs
        outcome = self.script.pop(0) if self.script else {}
        self._outcome = outcome
        self.typed = outcome.get("typed", "")
        self.reopen = outcome.get("reopen", False)
        self.apply_backspace = outcome.get("apply_backspace", False)
        self.closed_empty = outcome.get("closed_empty", False)
        self._col = kwargs.get("col", 0)
        _StubPicker.instances.append(self)

    def run(self):
        return self._outcome.get("selected")


def _editor(monkeypatch, tmp_path, completions_for=None):
    import cshell2.tui as tui

    monkeypatch.setattr(tui, "InlinePicker", _StubPicker)
    _StubPicker.instances = []
    _StubPicker.script = []
    ed = LineEditor(
        history=History(tmp_path / "history"),
        get_completions=completions_for or (lambda line: ([], "", "")),
        get_prompt=lambda: "> ",
    )
    monkeypatch.setattr(ed, "_redraw", lambda: None)
    return ed


def _history_completion(value):
    return Completion(value=value, description="history", verbatim=True)


def test_apply_inserts_at_the_anchor_verbatim(monkeypatch, tmp_path):
    ed = _editor(monkeypatch, tmp_path)
    ed._buf, ed._cursor = "git com", 7

    ed._apply(_history_completion('commit -m "fix typo"'), "com")

    # Replaces the partial token, inserted as-is: no shell quoting of the
    # spaces, no trailing space added.
    assert ed._buf == 'git commit -m "fix typo"'
    assert ed._cursor == len(ed._buf)


def test_apply_at_the_anchor_of_a_quoted_token(monkeypatch, tmp_path):
    """The raw anchor includes the opening quote, so it is not left behind."""
    ed = _editor(monkeypatch, tmp_path)
    ed._buf, ed._cursor = "cat 'My Do", 10

    ed._apply(_history_completion("'My Documents/notes.txt'"), "My Do")

    assert ed._buf == "cat 'My Documents/notes.txt'"
    assert ed._cursor == len(ed._buf)


def test_apply_keeps_text_after_the_caret(monkeypatch, tmp_path):
    ed = _editor(monkeypatch, tmp_path)
    ed._buf, ed._cursor = "git com --amend", 7

    ed._apply(_history_completion("commit -m msg"), "com")

    assert ed._buf == "git commit -m msg --amend"
    assert ed._cursor == len("git commit -m msg")


def test_unique_token_candidate_still_auto_applies(monkeypatch, tmp_path, capsys):
    """History must not cost a keystroke when one real candidate matches."""
    def completions_for(line):
        return ([_history_completion("alphabet soup"),
                 Completion(value="alpha")], "al", "")

    ed = _editor(monkeypatch, tmp_path, completions_for)
    ed._buf, ed._cursor = "ls al", 5

    ed._complete(0)

    assert ed._buf == "ls alpha "
    assert _StubPicker.instances == []   # no picker was ever opened
    capsys.readouterr()


def test_history_only_list_opens_a_picker_instead_of_auto_applying(
    monkeypatch, tmp_path, capsys
):
    """One history match must never rewrite the line without being shown."""
    def completions_for(line):
        return ([_history_completion("alphabet soup")], "al", "history")

    ed = _editor(monkeypatch, tmp_path, completions_for)
    ed._buf, ed._cursor = "ls al", 5
    _StubPicker.script = [{"selected": None}]

    ed._complete(0)

    assert len(_StubPicker.instances) == 1
    assert ed._buf == "ls al"            # dismissed → line untouched
    capsys.readouterr()


def test_selecting_a_history_row_extends_the_line(monkeypatch, tmp_path, capsys):
    entry = _history_completion("alphabet soup")

    def completions_for(line):
        return ([entry, Completion(value="alpha"), Completion(value="alps")], "al", "")

    ed = _editor(monkeypatch, tmp_path, completions_for)
    ed._buf, ed._cursor = "ls al", 5
    _StubPicker.script = [{"selected": entry}]

    ed._complete(0)

    assert ed._buf == "ls alphabet soup"
    capsys.readouterr()


def test_picker_alignment_covers_history_rows():
    """Anchored history rows align under the typed token like any candidate."""
    token_only = [Completion(value="alpha"), Completion(value="alps")]
    with_history = [_history_completion("alphabet soup")] + token_only
    assert _picker_col_offset("al", with_history) == _picker_col_offset("al", token_only)

    # A history-only list still aligns — the values start at the anchor, so the
    # picker opens under the "al" rather than back at the caret.
    assert _picker_col_offset("al", [_history_completion("alphabet soup")]) == 2


def test_picker_alignment_falls_back_to_token_rows_for_a_quoted_token():
    """A raw-quoted history display shares nothing with the shlex prefix."""
    quoted = [Completion(value="My Documents/")]
    assert _picker_col_offset("My Do", quoted) == 5
    # Measuring the history row alongside it would collapse the offset to 0 and
    # un-align the token row.
    assert _picker_col_offset(
        "My Do", [_history_completion("'My Documents/notes.txt'")] + quoted
    ) == 5
    # Nothing to fall back to when every row is quoted-verbatim.
    assert _picker_col_offset(
        "My Do", [_history_completion("'My Documents/notes.txt'")]
    ) == 0


# ---------------------------------------------------------------------------
# InlinePicker — TAB-extend hooks
# ---------------------------------------------------------------------------

def test_tab_extend_defers_to_extend_fn(capsys):
    """``extend_fn`` owns the arithmetic — the picker just types what it returns."""
    items = [_history_completion('commit -m "one"'), _history_completion("commit --amend")]
    picker = InlinePicker(items, extend_fn=lambda its, typed: "mit -")

    assert picker._handle_tab_complete() is True
    assert picker._typed == "mit -"
    assert picker.reopen
    capsys.readouterr()


def test_tab_extend_is_inert_when_extend_fn_returns_nothing(capsys):
    picker = InlinePicker(
        [_history_completion("alphabet soup")], extend_fn=lambda its, typed: ""
    )

    assert picker._handle_tab_complete() is False
    assert picker._typed == ""
    assert not picker.reopen
    capsys.readouterr()


def test_tab_extend_value_fn_fallback_still_works(capsys):
    """The simple hook (used by the flag-value picker) is unchanged."""
    picker = InlinePicker(
        [Completion(value="alpha"), Completion(value="alphb")],
        value_fn=lambda c: c.value,
        completion_prefix="al",
    )

    assert picker._handle_tab_complete() is True
    assert picker._typed == "ph"
    capsys.readouterr()


# ---------------------------------------------------------------------------
# LineEditor._complete — TAB-extend arithmetic across the two value spaces
# ---------------------------------------------------------------------------

def _extend_after_typing(monkeypatch, tmp_path, capsys, buf, completions_for, typed):
    """Open a completion picker on *buf*, then replay *typed* as the picker does.

    Returns ``(items, extension)``: the narrowed candidate list the picker would
    be showing and what a TAB press at that point would type.
    """
    ed = _editor(monkeypatch, tmp_path, completions_for)
    ed._buf, ed._cursor = buf, len(buf)
    _StubPicker.script = [{"selected": None}]

    ed._complete(0)
    capsys.readouterr()

    kwargs = _StubPicker.instances[0].kwargs
    items, _ = kwargs["refresh_fn"](typed)   # the picker narrows on every keystroke
    return items, kwargs["extend_fn"](items, typed)


def test_extend_uses_history_rows_once_narrowing_drops_the_token_rows(
    monkeypatch, tmp_path, capsys
):
    """The value space is re-decided per TAB, not frozen when the picker opened.

    ``make job <TAB>`` opens a mixed picker (files + history); typing ``J``
    leaves only the history rows, whose common prefix must still be typeable.
    """
    history = [_history_completion("JOB=alpha-1"), _history_completion("JOB=beta-2")]

    def completions_for(line):
        if line.endswith(" "):
            return (history + [Completion(value="Makefile"),
                               Completion(value="Makefile.old")], "", "")
        return (history, "J", "history")

    items, extension = _extend_after_typing(
        monkeypatch, tmp_path, capsys, "make job ", completions_for, "J"
    )

    assert all(c.verbatim for c in items)
    assert extension == "OB="


def test_extend_follows_the_anchor_when_typing_crosses_a_token_boundary(
    monkeypatch, tmp_path, capsys
):
    """A history row keeps matching past a space, which moves the anchor.

    Measuring the new candidates against the anchor the picker opened at
    over-counts by a whole token, which silently made TAB do nothing.
    """
    def completions_for(line):
        if line == "make jo":
            return ([_history_completion("job JOB=alpha-1"),
                     _history_completion("job JOB=beta-2")], "jo", "history")
        return ([_history_completion("JOB=alpha-1"),
                 _history_completion("JOB=beta-2")], "", "history")

    _, extension = _extend_after_typing(
        monkeypatch, tmp_path, capsys, "make jo", completions_for, "b J"
    )

    assert extension == "OB="


def test_extend_measures_token_rows_and_ignores_history_rows(
    monkeypatch, tmp_path, capsys
):
    """Mixed list: the extension is the token candidates' common prefix."""
    def completions_for(line):
        return ([_history_completion("alphabet soup"),
                 Completion(value="alpha"),
                 Completion(value="alphb")], "al", "")

    _, extension = _extend_after_typing(
        monkeypatch, tmp_path, capsys, "ls al", completions_for, ""
    )

    assert extension == "ph"
