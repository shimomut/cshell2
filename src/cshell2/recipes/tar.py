"""Completion recipe for tar."""

from __future__ import annotations

import os
import shutil

from ..commands import registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    DirCompleter,
    FileCompleter,
    OptionsCompleter,
    _to_slash,
)

# Single-letter flags that legitimately appear in a BSD-style bundle like
# ``cvzf``.  Action letters (c/x/t/r/u) are required for a bundle to qualify.
# ``f`` consumes a following positional (the archive); the rest are pure
# modifiers.  We deliberately exclude ``C`` and ``b`` because they consume
# their own value tokens and are rarely bundled — recognising them here would
# require modelling a slot-shift count, which is not worth the complexity.
_BUNDLE_LETTERS = set("cxtruvzjJZpkmOwhPsf")
_ACTION_LETTERS = set("cxtru")


def _decode_bundle(token: str) -> set[str] | None:
    """Return the set of letters in *token* if it is a tar bundle, else ``None``.

    Recognised forms (case-sensitive on the action letter):

        cvzf      → {'c','v','z','f'}     (BSD/SysV: no leading dash)
        -cvzf     → {'c','v','z','f'}     (dashed short-flag cluster)

    A bundle must contain at least one action letter and consist entirely of
    letters in ``_BUNDLE_LETTERS``.  Long options (``--foo``) and paths are
    rejected.
    """
    if not token or token.startswith("--"):
        return None
    body = token[1:] if token.startswith("-") else token
    if not body:
        return None
    if not all(c in _BUNDLE_LETTERS for c in body):
        return None
    if not (set(body) & _ACTION_LETTERS):
        return None
    return set(body)


TAR_OPTIONS: dict[str, str] = {
    "-c": "create archive",
    "-x": "extract from archive",
    "-t": "list archive contents",
    "-r": "append files to existing archive",
    "-u": "update — only newer files",
    "-f": "archive file path",
    "-v": "verbose",
    "-z": "compress with gzip",
    "-j": "compress with bzip2",
    "-J": "compress with xz",
    "--lzma": "compress with lzma",
    "-Z": "compress with compress(1)",
    "-C": "change to directory before processing",
    "-p": "preserve permissions on extract",
    "-k": "keep existing files (don't overwrite)",
    "-m": "don't restore modification times",
    "-O": "write to stdout, don't restore to disk",
    "-w": "interactive — confirm each file",
    "-h": "follow symlinks",
    "-P": "preserve absolute paths (don't strip leading /)",
    "-s": "modify file/archive names with substitution",
    "--exclude": "skip files matching pattern",
    "--include": "include only files matching pattern",
    "--strip-components": "strip leading components from paths",
    "--no-recursion": "don't recurse into directories",
    "--format": "select archive format",
    "--totals": "print total bytes after archive",
    "--newer": "only files newer than DATE",
    "--newer-mtime": "only files with mtime newer than DATE",
}

TAR_ARGS: dict[str, str | tuple[str, Completer]] = {
    "-f": ("ARCHIVE", FileCompleter()),
    "-C": ("DIR", DirCompleter()),
    "-b": "BLOCKING",
    "-s": "PATTERN",
    "--exclude": "PATTERN",
    "--include": "PATTERN",
    "--strip-components": "N",
    "--format": "FORMAT",
    "--newer": "DATE",
    "--newer-mtime": "DATE",
}


class TarArchiveCompleter(Completer):
    """Completes filesystem paths but lists tar-like archives first.

    Recognizes .tar, .tar.gz, .tgz, .tar.bz2, .tbz, .tbz2, .tar.xz, .txz,
    .tar.zst, .tzst, .tar.lzma, and bare .gz/.bz2/.xz/.zst when used with -f.
    """

    ARCHIVE_SUFFIXES = (
        ".tar",
        ".tar.gz", ".tgz",
        ".tar.bz2", ".tbz", ".tbz2",
        ".tar.xz", ".txz",
        ".tar.zst", ".tzst",
        ".tar.lzma", ".tar.lz",
    )

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if prefix:
            expanded = os.path.expanduser(prefix)
            directory = os.path.dirname(expanded) or "."
            partial = os.path.basename(expanded)
        else:
            directory = "."
            partial = ""
        try:
            entries = os.listdir(directory)
        except OSError:
            return []
        dirs: list[Completion] = []
        archives: list[Completion] = []
        others: list[Completion] = []
        for entry in sorted(entries):
            if entry.startswith(".") and not partial.startswith("."):
                continue
            if not entry.lower().startswith(partial.lower()):
                continue
            full = os.path.join(directory, entry)
            display_path = (
                os.path.join(os.path.dirname(prefix), entry)
                if prefix and os.path.dirname(prefix)
                else entry
            )
            display_path = _to_slash(display_path)
            if os.path.isdir(full):
                dirs.append(Completion(value=display_path + "/", display=entry + "/"))
            elif entry.lower().endswith(self.ARCHIVE_SUFFIXES):
                archives.append(Completion(value=display_path, display=entry))
            else:
                others.append(Completion(value=display_path, display=entry))
        return dirs + archives + others


class _TarPositionalCompleter(Completer):
    """Smart positional completer for ``tar`` that understands flag bundles.

    BSD-style ``tar cvzf a.tgz ./doc`` and the equivalent ``tar -cvzf a.tgz
    ./doc`` both pack action and modifier letters into one token.  When that
    bundle contains ``f``, the *next* positional slot is the archive name; the
    remaining positionals are member files.  Without ``f``, no archive slot
    exists and every positional is a member.

    A single instance is registered for every integer key in the completers
    dict (via :class:`_TarCompletersDict`); it inspects ``ctx.args`` to pick
    the right delegate per call.
    """

    def __init__(self) -> None:
        self._archive = TarArchiveCompleter()
        self._files = FileCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if self._is_archive_slot(ctx.args):
            return self._archive.complete(ctx)
        return self._files.complete(ctx)

    @staticmethod
    def _is_archive_slot(args: list[str]) -> bool:
        if not args:
            return True  # bare `tar <TAB>` → archive (legacy behaviour)
        bundle = _decode_bundle(args[0])
        if bundle is not None:
            if "f" not in bundle:
                return False  # bundle without 'f' has no archive slot at all
            archive_rank = 0 if args[0].startswith("-") else 1
        else:
            if "-f" in args:
                return False  # archive was already given via `-f ARCHIVE`
            archive_rank = 0
        # Count positional args while skipping flags (boolean: 1 token,
        # value-taking: 2 tokens).  Mirrors shell._positional_index so that
        # mixed forms like `tar cvzf -C /tmp <TAB>` correctly land on the
        # archive slot rather than counting `/tmp` as a positional.
        value_taking = set(TAR_ARGS)
        rank = 0
        i = 0
        while i < len(args):
            tok = args[i]
            if tok.startswith("--") or (tok.startswith("-") and len(tok) > 1):
                if tok in value_taking:
                    i += 2
                else:
                    i += 1
                continue
            rank += 1
            i += 1
        return rank == archive_rank


class _TarOptionsCompleter(OptionsCompleter):
    """Options completer that also marks letters from a bundle as used.

    After ``tar cvzf a.tgz`` the user typing ``-<TAB>`` should not see ``-c``,
    ``-v``, ``-z`` or ``-f`` listed again — they were already supplied via the
    bundle.  The base class only inspects dashed flags, so we extend the used
    set with every letter found in a leading bundle (BSD or dashed).
    """

    def _used_flags(self, ctx: CompletionContext) -> set[str]:
        used = super()._used_flags(ctx)
        if ctx.args:
            bundle = _decode_bundle(ctx.args[0])
            if bundle:
                for letter in bundle:
                    used.add(f"-{letter}")
        return used


class _TarCompletersDict(dict):
    """Completers map that routes every integer key to the smart positional.

    The shell's dispatch chain calls ``completers.get(None)`` for flag
    completion and ``completers.get(<positional_index>)`` for value
    completion.  We serve the same :class:`_TarPositionalCompleter` for every
    positional slot — it inspects ``ctx.args`` itself to decide between
    archive-aware and member-file completion.
    """

    def __init__(self, options_completer: OptionsCompleter, positional: Completer) -> None:
        super().__init__({None: options_completer})
        self._positional = positional

    def get(self, key, default=None):
        if isinstance(key, int):
            return self._positional
        return super().get(key, default)


def register() -> None:
    if shutil.which("tar") is None:
        return
    # Patch -f to use the archive-aware completer.
    args = dict(TAR_ARGS)
    args["-f"] = ("ARCHIVE", TarArchiveCompleter())
    options = _TarOptionsCompleter(TAR_OPTIONS, args=args)
    positional = _TarPositionalCompleter()
    command_registry.register_external_completers(
        "tar", _TarCompletersDict(options, positional),
        description="create, extract, or list tar archives",
    )
