"""Completion recipe for ssh."""

from __future__ import annotations

import os
import re

from ..commands import arg, registry as command_registry
from ..completion import Completer, Completion, CompletionContext, OptionsCompleter

SSH_OPTIONS: dict[str, str] = {
    "-4": "force IPv4 addresses only",
    "-6": "force IPv6 addresses only",
    "-A": "enable forwarding of the authentication agent connection",
    "-a": "disable forwarding of the authentication agent connection",
    "-C": "compress all data",
    "-f": "go to background just before command execution",
    "-G": "print the configuration and exit",
    "-i": "path to identity file (private key)",
    "-J": "connect via a jump host (ProxyJump)",
    "-L": "forward local port to remote side",
    "-l": "specify login name",
    "-N": "do not execute a remote command (for port forwarding)",
    "-n": "redirect stdin from /dev/null",
    "-p": "port to connect to on the remote host",
    "-q": "quiet mode — suppress warnings and diagnostic messages",
    "-R": "forward remote port to local side",
    "-T": "disable pseudo-terminal allocation",
    "-t": "force pseudo-terminal allocation",
    "-v": "verbose mode (use -vvv for more verbosity)",
    "-W": "forward stdio to host:port over secure channel",
    "-X": "enable X11 forwarding",
    "-x": "disable X11 forwarding",
    "-Y": "enable trusted X11 forwarding",
}

SSH_ARGS: dict[str, str] = {
    "-i": "FILE",
    "-J": "HOST",
    "-L": "[BIND:]PORT:HOST:HOSTPORT",
    "-l": "USER",
    "-p": "PORT",
    "-R": "[BIND:]PORT:HOST:HOSTPORT",
    "-W": "HOST:PORT",
}


class SSHHostCompleter(Completer):
    """Completes SSH hostnames from ~/.ssh/config and ~/.ssh/known_hosts."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        hosts: dict[str, str] = {}
        self._load_ssh_config(hosts)
        self._load_known_hosts(hosts)
        prefix = ctx.prefix
        return [
            Completion(value=h, description=desc)
            for h, desc in sorted(hosts.items())
            if h.startswith(prefix)
        ]

    def _load_ssh_config(self, hosts: dict[str, str]) -> None:
        config_path = os.path.expanduser("~/.ssh/config")
        try:
            with open(config_path) as f:
                content = f.read()
        except OSError:
            return
        for line in content.splitlines():
            m = re.match(r"^\s*Host\s+(.+)", line, re.IGNORECASE)
            if m:
                for h in m.group(1).split():
                    if "*" not in h and "?" not in h:
                        hosts[h] = "~/.ssh/config"

    def _load_known_hosts(self, hosts: dict[str, str]) -> None:
        known_hosts_path = os.path.expanduser("~/.ssh/known_hosts")
        try:
            with open(known_hosts_path) as f:
                lines = f.readlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("|"):
                continue
            host_part = line.split()[0]
            for h in host_part.split(","):
                h = h.strip()
                # Strip [host]:port format
                m = re.match(r"^\[(.+)\]:\d+$", h)
                if m:
                    h = m.group(1)
                if h and "*" not in h and h not in hosts:
                    hosts[h] = "known_hosts"


def register() -> None:
    command_registry.command(
        "ssh",
        help="OpenSSH remote login client",
        params=[
            arg("host", help="hostname or user@host", completer=SSHHostCompleter()),
        ],
        options_completer=OptionsCompleter(SSH_OPTIONS, args=SSH_ARGS),
    )
