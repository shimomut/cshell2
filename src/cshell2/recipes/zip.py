"""Completion recipe for zip."""

from __future__ import annotations

import shutil

from ..commands import registry as command_registry
from ..completion import FileCompleter, OptionsCompleter

ZIP_OPTIONS: dict[str, str] = {
    "-r": "recurse into directories",
    "-q": "quiet operation",
    "-v": "verbose / show version info",
    "-u": "update — only changed or new files",
    "-f": "freshen — only changed files",
    "-d": "delete entries from zipfile",
    "-m": "move into zipfile (delete OS files after)",
    "-j": "junk paths (don't record directory names)",
    "-0": "store only (no compression)",
    "-1": "compress faster",
    "-9": "compress better",
    "-c": "add one-line comments",
    "-z": "add zipfile comment",
    "-T": "test zipfile integrity",
    "-x": "exclude the following names",
    "-i": "include only the following names",
    "-D": "do not add directory entries",
    "-X": "exclude extra file attributes",
    "-y": "store symlinks as the link, not the referenced file",
    "-e": "encrypt — prompt for password",
    "-n": "don't compress files with these suffixes",
    "-o": "make zipfile as old as latest entry",
    "-@": "read names from stdin",
    "-h": "show help",
    "-h2": "show extended help",
    "-L": "show software license",
    "-A": "adjust self-extracting executable",
    "-F": "fix zipfile",
    "-FF": "try harder to fix zipfile",
    "-J": "junk zipfile prefix (unzipsfx)",
}

ZIP_ARGS: dict[str, str] = {
    "-n": "SUFFIX_LIST",
    "-t": "MMDDYYYY",
    "-b": "PATH",
}


def register() -> None:
    if shutil.which("zip") is None:
        return
    command_registry.register_external_completers("zip", {
        None: OptionsCompleter(ZIP_OPTIONS, args=ZIP_ARGS),
        0: FileCompleter(),
        1: FileCompleter(),
        2: FileCompleter(),
        3: FileCompleter(),
        4: FileCompleter(),
    }, description="package and compress files into a zip archive")
