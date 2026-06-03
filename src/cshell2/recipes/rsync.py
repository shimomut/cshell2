"""Completion recipe for rsync.

The path completer handles plain local paths and accepts ``user@host:`` /
``host:`` prefixes — the path portion after ``:`` is left to the user
since enumerating remote paths requires an SSH round-trip per keystroke.
"""

from __future__ import annotations

import shutil

from ..commands import registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    FileCompleter,
    OptionsCompleter,
)
from .ssh import SSHHostCompleter

RSYNC_OPTIONS: dict[str, str] = {
    "-a": "archive — equivalent to -rlptgoD",
    "-v": "verbose",
    "-q": "quiet — suppress non-error messages",
    "-r": "recurse into directories",
    "-l": "copy symlinks as symlinks",
    "-L": "transform symlinks into the referenced file/dir",
    "-p": "preserve permissions",
    "-t": "preserve modification times",
    "-g": "preserve group",
    "-o": "preserve owner (super-user only)",
    "-D": "preserve device + special files",
    "-H": "preserve hard links",
    "-A": "preserve ACLs (implies -p)",
    "-X": "preserve extended attributes",
    "-S": "handle sparse files efficiently",
    "-x": "don't cross filesystem boundaries",
    "-c": "skip based on checksum, not mod-time + size",
    "-n": "dry run — show what would be done",
    "-u": "skip files that are newer on the receiver",
    "-z": "compress file data during transfer",
    "-h": "human-readable numbers",
    "-i": "show itemized changes for each transfer",
    "-P": "same as --partial --progress",
    "-e": "specify the remote shell to use (e.g. 'ssh -p 2222')",
    "-W": "copy whole files (no incremental delta-transfer)",
    "-R": "use relative path names",
    "-b": "make backups (see --suffix and --backup-dir)",
    "--delete": "delete extraneous files from destination dirs",
    "--delete-before": "receiver deletes before transfer (default)",
    "--delete-after": "receiver deletes after transfer, not before",
    "--delete-excluded": "also delete excluded files from destination",
    "--exclude": "exclude files matching PATTERN",
    "--exclude-from": "read exclude patterns from FILE",
    "--include": "include files matching PATTERN",
    "--include-from": "read include patterns from FILE",
    "--filter": "add a filter rule",
    "--files-from": "read list of source-file names from FILE",
    "--from0": "all *-from / files-from file lists are NUL-terminated",
    "--checksum": "skip based on checksum, not mod-time + size",
    "--archive": "archive — equivalent to -a",
    "--no-implied-dirs": "do not create implied directories with --relative",
    "--existing": "skip creating new files on destination",
    "--ignore-existing": "skip updating files that already exist",
    "--remove-source-files": "delete files on source after successful transfer",
    "--partial": "keep partially transferred files",
    "--partial-dir": "put partial transfers in DIR",
    "--progress": "show progress during transfer",
    "--stats": "give some file-transfer stats",
    "--bwlimit": "limit socket I/O bandwidth (KBps)",
    "--max-size": "skip files larger than SIZE",
    "--min-size": "skip files smaller than SIZE",
    "--timeout": "I/O timeout in seconds",
    "--port": "specify daemon port (default 873)",
    "--password-file": "read daemon password from FILE",
    "--rsh": "remote shell command (same as -e)",
    "--rsync-path": "rsync command to run on remote",
    "--compress-level": "explicitly set compression level",
    "--dry-run": "show what would be done without doing it",
    "--itemize-changes": "show itemized changes",
    "--info": "fine-grained informational verbosity",
    "--debug": "fine-grained debug verbosity",
    "--help": "show help",
    "--version": "show version",
}

RSYNC_ARGS: dict[str, object] = {
    "-e": "REMOTE-SHELL",
    "--rsh": "REMOTE-SHELL",
    "--rsync-path": "PROGRAM",
    "--exclude": "PATTERN",
    "--exclude-from": ("FILE", FileCompleter()),
    "--include": "PATTERN",
    "--include-from": ("FILE", FileCompleter()),
    "--filter": "RULE",
    "--files-from": ("FILE", FileCompleter()),
    "--partial-dir": "DIR",
    "--bwlimit": "KBPS",
    "--max-size": "SIZE",
    "--min-size": "SIZE",
    "--timeout": "SECONDS",
    "--port": "PORT",
    "--password-file": ("FILE", FileCompleter()),
    "--compress-level": "LEVEL",
    "--info": "FLAGS",
    "--debug": "FLAGS",
    "--backup-dir": "DIR",
    "--suffix": "SUFFIX",
}


class _RemoteOrFileCompleter(Completer):
    """Completes ``user@host:`` / ``host:`` prefixes from SSH config; otherwise files.

    Users typing ``host:`` after a ``:`` get no completion for the remote
    path (would require an SSH round-trip).
    """

    def __init__(self) -> None:
        self._files = FileCompleter()
        self._hosts = SSHHostCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if ":" in prefix:
            # Already past the colon — leave the remote-path part to the user.
            return []
        if "@" in prefix and "/" not in prefix:
            # Looks like "user@<TAB>" — complete the host portion.
            user_part, partial = prefix.split("@", 1)
            host_ctx = CompletionContext(
                command=ctx.command,
                args=ctx.args,
                arg_index=ctx.arg_index,
                prefix=partial,
                line=ctx.line,
                shell_context=ctx.shell_context,
            )
            host_completions = self._hosts.complete(host_ctx)
            return [
                Completion(
                    value=f"{user_part}@{c.value}:",
                    display=f"{user_part}@{c.value}",
                    description=c.description,
                )
                for c in host_completions
            ]
        # Plain prefix: offer files first, then hosts (with trailing ':' to
        # signal a remote target).
        results = self._files.complete(ctx)
        if not prefix or not prefix.startswith((".", "/", "~")):
            for c in self._hosts.complete(ctx):
                results.append(Completion(
                    value=f"{c.value}:",
                    display=f"{c.value}:",
                    description=c.description,
                ))
        return results


def register() -> None:
    if shutil.which("rsync") is None:
        return
    completer = _RemoteOrFileCompleter()
    command_registry.register_external_completers("rsync", {
        None: OptionsCompleter(RSYNC_OPTIONS, args=RSYNC_ARGS),
        0: completer,
        1: completer,
        2: completer,
        3: completer,
        4: completer,
    })
