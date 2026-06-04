"""Completion recipe for tar."""

from __future__ import annotations

import os
import shutil

from ..commands import arg, flag_args, registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    DirCompleter,
    FileCompleter,
    _to_slash,
)


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
    "-f": ("ARCHIVE", FileCompleter()),  # patched to TarArchiveCompleter in register()
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
    """Smart positional completer for ``tar``.

    The first positional is the archive (use the archive-aware completer);
    every subsequent positional is a member file (plain file completion).

    When the archive was supplied via standalone ``-f ARCHIVE`` instead of
    appearing as a positional, every positional is a member.  Note that
    short-flag clusters like ``-cvzf`` don't consume the next token as the
    archive — they're treated as a single boolean flag, so the archive
    still lands at positional 0.
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
        if "-f" in args:
            # Standalone -f consumed the next token as the archive — all
            # subsequent positionals are members.
            return False
        # Count completed positionals, skipping flags and their values.
        value_taking = set(TAR_ARGS)
        rank = 0
        i = 0
        while i < len(args):
            tok = args[i]
            if tok.startswith("-"):
                i += 2 if tok in value_taking else 1
            else:
                rank += 1
                i += 1
        return rank == 0


def register() -> None:
    if shutil.which("tar") is None:
        return
    # Patch -f to use the archive-aware completer.
    tar_flag_values = dict(TAR_ARGS)
    tar_flag_values["-f"] = ("ARCHIVE", TarArchiveCompleter())
    command_registry.command(
        "tar",
        help="create, extract, or list tar archives",
        params=[
            arg("path", nargs="*", help="archive or member file",
                completer=_TarPositionalCompleter()),
            *flag_args(TAR_OPTIONS, values=tar_flag_values),
        ],
    )
