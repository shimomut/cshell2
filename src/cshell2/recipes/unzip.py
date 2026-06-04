"""Completion recipe for unzip."""

from __future__ import annotations

import os
import shutil

from ..commands import arg, registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    DirCompleter,
    FileCompleter,
    OptionsCompleter,
    _to_slash,
)

UNZIP_OPTIONS: dict[str, str] = {
    "-Z": "ZipInfo mode — archive listing",
    "-p": "extract files to pipe (no messages)",
    "-l": "list files (short format)",
    "-f": "freshen existing files, create none",
    "-t": "test compressed archive data",
    "-u": "update files, create if necessary",
    "-z": "display archive comment only",
    "-v": "list verbosely / show version info",
    "-T": "set timestamp on archive to latest",
    "-x": "exclude files that follow",
    "-d": "extract files into the given directory",
    "-n": "never overwrite existing files",
    "-q": "quiet mode (-qq for quieter)",
    "-o": "overwrite files WITHOUT prompting",
    "-a": "auto-convert text files",
    "-aa": "treat ALL files as text",
    "-j": "junk paths (do not make directories)",
    "-C": "match filenames case-insensitively",
    "-L": "make (some) names lowercase",
    "-X": "restore UID/GID info",
    "-V": "retain VMS version numbers",
    "-K": "keep setuid/setgid/sticky permissions",
    "-M": "pipe output through 'more' pager",
    "-P": "use PASSWORD to decrypt entries",
    "-h": "show help",
    "-hh": "show extended help",
}

UNZIP_ARGS: dict[str, str] = {
    "-d": ("DIR", DirCompleter()),
    "-P": "PASSWORD",
}


class ZipArchiveCompleter(Completer):
    """Completes filesystem paths but lists *.zip files first."""

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
        zips: list[Completion] = []
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
            elif entry.lower().endswith(".zip"):
                zips.append(Completion(value=display_path, display=entry))
            else:
                others.append(Completion(value=display_path, display=entry))
        return dirs + zips + others


def register() -> None:
    if shutil.which("unzip") is None:
        return
    command_registry.command(
        "unzip",
        help="list, test, or extract files from a zip archive",
        params=[
            arg("archive", help="zip archive", completer=ZipArchiveCompleter()),
            arg("file", nargs="*", help="member file to extract", completer=FileCompleter()),
        ],
        options_completer=OptionsCompleter(UNZIP_OPTIONS, args=UNZIP_ARGS),
    )
