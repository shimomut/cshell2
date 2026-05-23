"""Completion recipe for find."""

from __future__ import annotations

from ..commands import registry
from ..completion import FileCompleter, OptionsCompleter

FIND_OPTIONS: dict[str, str] = {
    # Output / actions
    "-delete": "delete matched files (implies -depth)",
    "-exec": "execute command for each matched file",
    "-execdir": "like -exec but run command in file's directory",
    "-ls": "list matched files as if by ls -dils",
    "-print": "print the full path of matched files (default)",
    "-print0": "print path followed by a null byte (for xargs -0)",
    # Filters — name / path
    "-iname": "like -name but case-insensitive",
    "-ipath": "like -path but case-insensitive",
    "-iregex": "like -regex but case-insensitive",
    "-name": "base of filename matches shell pattern",
    "-path": "file path matches shell pattern",
    "-regex": "file path matches regular expression",
    # Filters — type / permissions
    "-empty": "file is empty and is a regular file or directory",
    "-executable": "file is executable by the current user",
    "-group": "file belongs to the named group",
    "-perm": "file permission bits match mode",
    "-readable": "file is readable by the current user",
    "-type": "file type: f=file d=directory l=symlink b=block c=char p=pipe s=socket",
    "-user": "file is owned by the named user",
    "-writable": "file is writable by the current user",
    # Filters — time
    "-atime": "file was last accessed n*24 hours ago",
    "-ctime": "file status was last changed n*24 hours ago",
    "-mtime": "file was last modified n*24 hours ago",
    "-newer": "file was modified more recently than the given file",
    "-newermt": "file was modified more recently than given timestamp",
    # Filters — size
    "-size": "file uses n units of space (k=KB M=MB G=GB)",
    # Traversal
    "-depth": "process directory contents before the directory itself",
    "-follow": "dereference symbolic links (deprecated, prefer -L)",
    "-maxdepth": "descend at most n levels of directories",
    "-mindepth": "do not apply tests at levels less than n",
    "-mount": "do not descend directories on other filesystems",
    "-xdev": "do not descend directories on other filesystems (alias)",
    # Logical operators
    "-not": "negate the following expression",
    "-or": "logical OR of two expressions",
    "-and": "logical AND of two expressions (default between expressions)",
    "-prune": "do not descend into matched directory",
}


FIND_ARGS: dict[str, str] = {
    "-name": "PATTERN",
    "-iname": "PATTERN",
    "-path": "PATTERN",
    "-ipath": "PATTERN",
    "-regex": "PATTERN",
    "-iregex": "PATTERN",
    "-maxdepth": "N",
    "-mindepth": "N",
    "-mtime": "N",
    "-atime": "N",
    "-ctime": "N",
    "-newer": "FILE",
    "-newermt": "TIMESTAMP",
    "-size": "N[ckMG]",
    "-type": "f|d|l|b|c|p|s",
    "-perm": "MODE",
    "-user": "NAME",
    "-group": "NAME",
    "-exec": "CMD {} \\;",
    "-execdir": "CMD {} \\;",
}


def register() -> None:
    registry.register_external_completers("find", {
        None: OptionsCompleter(FIND_OPTIONS, args=FIND_ARGS),
        0: FileCompleter(),
        1: FileCompleter(),
    })
