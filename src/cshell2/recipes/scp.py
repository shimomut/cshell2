"""Completion recipe for scp.

Source/destination completion handles ``user@host:path`` / ``host:path``
prefixes via the SSH host completer; the path portion after ``:`` is left
to the user (would require a remote round-trip per keystroke).
"""

from __future__ import annotations

import shutil

from ..commands import arg, flag_args, registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    FileCompleter,
)
from .ssh import SSHHostCompleter

SCP_OPTIONS: dict[str, str] = {
    "-3": "transfer between two remote hosts via the local machine",
    "-4": "force IPv4",
    "-6": "force IPv6",
    "-A": "forward authentication agent connection",
    "-B": "batch mode — disables passphrase/password prompts",
    "-C": "compress data",
    "-c": "select cipher",
    "-D": "use sftp protocol with specific port",
    "-F": "alternative ssh_config file",
    "-i": "identity (private key) file",
    "-J": "ProxyJump host",
    "-l": "limit bandwidth (Kbit/s)",
    "-O": "use the legacy SCP protocol (vs SFTP)",
    "-o": "ssh option (KEY=VALUE)",
    "-P": "remote port to connect to",
    "-p": "preserve modification times, access times, modes",
    "-q": "quiet mode — disables progress meter",
    "-R": "transfer between two remote hosts directly (RFC 8341)",
    "-r": "recursively copy directories",
    "-S": "program to use for the encrypted connection",
    "-T": "disable strict filename checking",
    "-v": "verbose",
}

SCP_ARGS: dict[str, object] = {
    "-c": "CIPHER",
    "-D": "PORT",
    "-F": ("CONFIG", FileCompleter()),
    "-i": ("KEY-FILE", FileCompleter()),
    "-J": "[USER@]HOST[:PORT]",
    "-l": "KBPS",
    "-o": "OPTION",
    "-P": "PORT",
    "-S": "PROGRAM",
}


class _RemoteOrFileCompleter(Completer):
    """Completes filesystem paths and ``[user@]host:`` prefixes for scp.

    A trailing ``:`` is appended so the user can chain a remote path after
    accepting a host completion.
    """

    def __init__(self) -> None:
        self._files = FileCompleter()
        self._hosts = SSHHostCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if ":" in prefix:
            return []
        if "@" in prefix and "/" not in prefix:
            user_part, partial = prefix.split("@", 1)
            host_ctx = CompletionContext(
                command=ctx.command,
                args=ctx.args,
                arg_index=ctx.arg_index,
                prefix=partial,
                line=ctx.line,
                shell_context=ctx.shell_context,
            )
            return [
                Completion(
                    value=f"{user_part}@{c.value}:",
                    display=f"{user_part}@{c.value}",
                    description=c.description,
                )
                for c in self._hosts.complete(host_ctx)
            ]
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
    if shutil.which("scp") is None:
        return
    command_registry.command(
        "scp",
        help="secure copy files between hosts over SSH",
        params=[
            arg("path", nargs="*", help="source or destination", completer=_RemoteOrFileCompleter()),
            *flag_args(SCP_OPTIONS, values=SCP_ARGS),
        ],
    )
