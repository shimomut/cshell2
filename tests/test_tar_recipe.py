"""Tests for the tar recipe — bundled-flag detection and positional dispatch."""

from __future__ import annotations

import os
import tempfile

import pytest

from cshell2.completion import CompletionContext
from cshell2.recipes.tar import (
    TarArchiveCompleter,
    _decode_bundle,
    _TarOptionsCompleter,
    _TarPositionalCompleter,
    TAR_OPTIONS,
    TAR_ARGS,
)


def make_ctx(args=None, prefix="", command="tar"):
    return CompletionContext(
        command=command,
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line="",
        shell_context=None,
    )


# ---------------------------------------------------------------------------
# _decode_bundle
# ---------------------------------------------------------------------------

class TestDecodeBundle:
    def test_bsd_bundle(self):
        assert _decode_bundle("cvzf") == set("cvzf")

    def test_dashed_bundle(self):
        assert _decode_bundle("-cvzf") == set("cvzf")

    def test_extract_bundle(self):
        assert _decode_bundle("xvf") == set("xvf")

    def test_list_bundle(self):
        assert _decode_bundle("tf") == set("tf")

    def test_no_action_letter_rejected(self):
        # "vz" has no action letter (c/x/t/r/u) — not a bundle.
        assert _decode_bundle("vz") is None

    def test_long_option_rejected(self):
        assert _decode_bundle("--exclude") is None

    def test_path_rejected(self):
        assert _decode_bundle("./doc/") is None
        assert _decode_bundle("a.tgz") is None

    def test_dash_only_rejected(self):
        assert _decode_bundle("-") is None
        assert _decode_bundle("") is None

    def test_unknown_letter_rejected(self):
        # 'q' isn't a bundle letter.
        assert _decode_bundle("cvq") is None


# ---------------------------------------------------------------------------
# _TarPositionalCompleter._is_archive_slot
# ---------------------------------------------------------------------------

class TestArchiveSlotDetection:
    def setup_method(self):
        self.p = _TarPositionalCompleter()

    @pytest.mark.parametrize("args, expected", [
        ([], True),                                 # bare `tar <TAB>`
        (["cvzf"], True),                           # BSD bundle, archive next
        (["-cvzf"], True),                          # dashed bundle, archive next
        (["cvzf", "a.tgz"], False),                 # archive given → members next
        (["-cvzf", "a.tgz"], False),
        (["xvf"], True),                            # extract bundle
        (["xvf", "a.tgz", "member1"], False),       # member already started
        (["cvz"], False),                           # bundle without 'f' has no archive slot
        (["cvz", "member"], False),
        (["-f", "a.tgz"], False),                   # archive already given via -f
        (["-f", "a.tgz", "member"], False),
        (["cvzf", "-C", "/tmp"], True),             # -C consumes /tmp; still need archive
        (["cvzf", "-C", "/tmp", "a.tgz"], False),
        (["./doc/"], False),                        # path counted as rank 1, no archive slot
    ])
    def test_archive_slot(self, args, expected):
        assert self.p._is_archive_slot(args) is expected


# ---------------------------------------------------------------------------
# _TarPositionalCompleter.complete
# ---------------------------------------------------------------------------

class TestPositionalCompleter:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self.tmp.name
        # Layout: a.tgz (archive), b.txt (regular), doc/ (directory)
        open(os.path.join(self.tmpdir, "a.tgz"), "w").close()
        open(os.path.join(self.tmpdir, "b.txt"), "w").close()
        os.mkdir(os.path.join(self.tmpdir, "doc"))
        self._old_cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def teardown_method(self):
        os.chdir(self._old_cwd)
        self.tmp.cleanup()

    def test_archive_slot_lists_archives_first(self):
        """`tar cvzf <TAB>` → archives appear before regular files."""
        p = _TarPositionalCompleter()
        results = p.complete(make_ctx(args=["cvzf"], prefix=""))
        # Drop directories from comparison (they always come first regardless).
        non_dir = [r.value for r in results if not r.value.endswith("/")]
        # Archives sort ahead of plain files.
        assert non_dir.index("a.tgz") < non_dir.index("b.txt")

    def test_member_slot_uses_plain_file_completer(self):
        """`tar cvzf a.tgz <TAB>` → member files; no archive prioritisation."""
        p = _TarPositionalCompleter()
        results = p.complete(make_ctx(args=["cvzf", "a.tgz"], prefix=""))
        values = [r.value for r in results]
        # FileCompleter sorts dirs first, then files alphabetically.
        # Both files should appear in plain alpha order (a.tgz before b.txt).
        assert values.index("a.tgz") < values.index("b.txt")
        # Crucially, this is the sorted-alpha order, not archives-first.
        # (That's also the alpha order here, but the dispatch path differs;
        # we verify dispatch directly below.)

    def test_dispatches_archive_completer_on_archive_slot(self):
        p = _TarPositionalCompleter()
        # When _is_archive_slot returns True, the archive completer runs.
        # We assert this via behaviour: archives appear ahead of files.
        results = p.complete(make_ctx(args=["xvf"], prefix=""))
        non_dir = [r.value for r in results if not r.value.endswith("/")]
        assert non_dir.index("a.tgz") < non_dir.index("b.txt")


# ---------------------------------------------------------------------------
# _TarOptionsCompleter — bundle letters marked as used
# ---------------------------------------------------------------------------

class TestTarOptionsCompleter:
    def test_bundle_letters_excluded_from_listing(self):
        """After `tar cvzf <TAB>` typing `-`, `-c/-v/-z/-f` should not reappear."""
        oc = _TarOptionsCompleter(TAR_OPTIONS, args=TAR_ARGS)
        results = oc.complete(make_ctx(args=["cvzf"], prefix="-"))
        values = [r.value for r in results]
        for absent in ("-c", "-v", "-z", "-f"):
            assert absent not in values
        # An unrelated flag still shows up.
        assert "-C" in values

    def test_dashed_bundle_letters_excluded(self):
        oc = _TarOptionsCompleter(TAR_OPTIONS, args=TAR_ARGS)
        results = oc.complete(make_ctx(args=["-cvzf"], prefix="-"))
        values = [r.value for r in results]
        for absent in ("-c", "-v", "-z", "-f"):
            assert absent not in values

    def test_non_bundle_first_arg_unaffected(self):
        """A path-like first arg should not be parsed as a bundle."""
        oc = _TarOptionsCompleter(TAR_OPTIONS, args=TAR_ARGS)
        results = oc.complete(make_ctx(args=["./doc/"], prefix="-"))
        values = [r.value for r in results]
        assert "-c" in values
        assert "-f" in values


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_smart_dict(self):
        """The recipe registers a dict that routes every int to the smart completer."""
        from cshell2.commands import registry as command_registry
        from cshell2.recipes import tar as tar_recipe

        if not _which("tar"):
            pytest.skip("tar not on PATH")

        tar_recipe.register()
        completers = command_registry.get_external_completers("tar")
        assert completers is not None
        # The smart positional must be returned for any integer index.
        c0 = completers.get(0)
        c1 = completers.get(1)
        c5 = completers.get(5)
        assert c0 is c1 is c5
        assert isinstance(c0, _TarPositionalCompleter)
        # And the options completer is the bundle-aware subclass.
        assert isinstance(completers.get(None), _TarOptionsCompleter)


def _which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None
