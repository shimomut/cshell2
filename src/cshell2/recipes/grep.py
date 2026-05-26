"""Completion recipe for grep / egrep / fgrep."""

from __future__ import annotations

from ..commands import registry
from ..completion import FileCompleter, OptionsCompleter

GREP_OPTIONS: dict[str, str] = {
    "-a": "process binary file as if it were text",
    "-c": "print only a count of matching lines",
    "-E": "interpret pattern as extended regular expression",
    "-F": "interpret pattern as fixed string, not regex",
    "-G": "interpret pattern as basic regular expression (default)",
    "-h": "suppress the prefixing of filenames on output",
    "-H": "print the filename with output lines",
    "-i": "perform case-insensitive matching",
    "-I": "process binary file as if it does not contain matches",
    "-l": "print only the names of files containing matches",
    "-L": "print only the names of files containing no matches",
    "-m": "stop reading after NUM matching lines",
    "-n": "prefix each output line with its line number",
    "-o": "print only the matched (non-empty) parts of a matching line",
    "-P": "interpret pattern as Perl-compatible regular expression",
    "-q": "do not write anything to stdout; exit 0 if match found",
    "-r": "read all files under each directory recursively",
    "-R": "like -r but follow all symbolic links",
    "-s": "suppress error messages about nonexistent or unreadable files",
    "-v": "invert the sense of matching",
    "-w": "select only lines containing matches that form whole words",
    "-x": "select only lines whose entire lines are matched",
    "-z": "treat input as a set of lines, each terminated by a zero byte",
    "-A": "print NUM lines of trailing context after each match",
    "-B": "print NUM lines of leading context before each match",
    "-C": "print NUM lines of output context",
    "--color": "mark up the matching text",
    "--include": "search only files whose name matches the given pattern",
    "--exclude": "skip files whose name matches the given pattern",
    "--exclude-dir": "skip directories whose name matches the given pattern",
}


GREP_ARGS: dict[str, str] = {
    "-A": "N",
    "-B": "N",
    "-C": "N",
    "-m": "N",
    "--include": "PATTERN",
    "--exclude": "PATTERN",
    "--exclude-dir": "PATTERN",
}


def register() -> None:
    completers = {
        None: OptionsCompleter(GREP_OPTIONS, args=GREP_ARGS),
        0: FileCompleter(),
        1: FileCompleter(),
        2: FileCompleter(),
    }
    for cmd in ("grep", "egrep", "fgrep", "rgrep"):
        registry.register_external_completers(cmd, completers)
