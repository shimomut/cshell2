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


def register() -> None:
    if shutil.which("tar") is None:
        return
    # Patch -f to use the archive-aware completer.
    args = dict(TAR_ARGS)
    args["-f"] = ("ARCHIVE", TarArchiveCompleter())
    command_registry.register_external_completers("tar", {
        None: OptionsCompleter(TAR_OPTIONS, args=args),
        0: TarArchiveCompleter(),
        1: FileCompleter(),
        2: FileCompleter(),
        3: FileCompleter(),
        4: FileCompleter(),
    }, description="create, extract, or list tar archives")
