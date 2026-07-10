"""Per-context Up/Down history vs. global Ctrl-R / on-disk store.

Up/Down navigation is scoped to the current context's in-memory list; the
global History (on disk, and the Ctrl-R source) collects every command from
every context.
"""

from pathlib import Path

from cshell2.lineedit import History, LineEditor


def _make_editor(history, local_list):
    return LineEditor(
        history=history,
        get_completions=lambda line: ([], "", ""),
        get_prompt=lambda: "> ",
        local_history_fn=lambda: local_list,
    )


def test_add_to_history_updates_both_global_and_local(tmp_path):
    hist = History(tmp_path / "history")
    local: list[str] = []
    ed = _make_editor(hist, local)

    ed.add_to_history("cmd one")
    ed.add_to_history("cmd two")

    assert hist.entries == ["cmd one", "cmd two"]
    assert local == ["cmd one", "cmd two"]


def test_local_dedup_consecutive(tmp_path):
    hist = History(tmp_path / "history")
    local: list[str] = []
    ed = _make_editor(hist, local)

    ed.add_to_history("same")
    ed.add_to_history("same")
    assert local == ["same"]


def test_updown_reads_local_not_global(tmp_path):
    # Global store already has entries from other contexts...
    path = tmp_path / "history"
    path.write_text("global-a\nglobal-b\n")
    hist = History(path)
    # ...but this context's local list is separate.
    local = ["local-x", "local-y"]
    ed = _make_editor(hist, local)

    ed._buf = ""
    ed._hist_back()
    assert ed._buf == "local-y"
    ed._hist_back()
    assert ed._buf == "local-x"
    # No more local entries — does not fall through to the global store.
    ed._hist_back()
    assert ed._buf == "local-x"


def test_ctrl_r_source_is_global(tmp_path):
    # The Ctrl-R picker builds from self._history.entries (global), not local.
    path = tmp_path / "history"
    path.write_text("global-a\nglobal-b\n")
    hist = History(path)
    ed = _make_editor(hist, ["local-only"])

    assert ed._history.entries == ["global-a", "global-b"]


def test_no_local_fn_falls_back_to_global(tmp_path):
    path = tmp_path / "history"
    path.write_text("g1\ng2\n")
    hist = History(path)
    ed = LineEditor(
        history=hist,
        get_completions=lambda line: ([], "", ""),
        get_prompt=lambda: "> ",
    )
    ed._buf = ""
    ed._hist_back()
    assert ed._buf == "g2"
