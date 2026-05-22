"""Completion recipe for docker."""

from __future__ import annotations

import subprocess

from ..commands import CommandRegistry
from ..completion import Completer, Completion, CompletionContext, OptionsCompleter

DOCKER_SUBCOMMANDS: dict[str, str] = {
    "build": "build an image from a Dockerfile",
    "commit": "create a new image from a container's changes",
    "cp": "copy files between a container and the local filesystem",
    "create": "create a new container",
    "exec": "run a command in a running container",
    "images": "list images",
    "inspect": "return low-level information on containers/images/volumes",
    "kill": "kill one or more running containers",
    "logs": "fetch the logs of a container",
    "network": "manage networks",
    "pause": "pause all processes within one or more containers",
    "ps": "list containers",
    "pull": "pull an image or a repository from a registry",
    "push": "push an image or a repository to a registry",
    "restart": "restart one or more containers",
    "rm": "remove one or more containers",
    "rmi": "remove one or more images",
    "run": "run a command in a new container",
    "start": "start one or more stopped containers",
    "stats": "display a live stream of container resource usage statistics",
    "stop": "stop one or more running containers",
    "tag": "create a tag targeting a source image",
    "top": "display the running processes of a container",
    "unpause": "unpause all processes within one or more containers",
    "volume": "manage volumes",
}

_SUBCOMMAND_OPTIONS: dict[str, dict[str, str]] = {
    "run": {
        "--detach": "run container in background and print container ID",
        "--env": "set environment variables",
        "--interactive": "keep STDIN open even if not attached",
        "--mount": "attach a filesystem mount to the container",
        "--name": "assign a name to the container",
        "--network": "connect a container to a network",
        "--publish": "publish a container's port(s) to the host",
        "--rm": "automatically remove the container when it exits",
        "--tty": "allocate a pseudo-TTY",
        "--volume": "bind mount a volume",
        "-d": "run container in background",
        "-e": "set environment variables",
        "-i": "keep STDIN open",
        "-p": "publish ports HOST:CONTAINER",
        "-t": "allocate a pseudo-TTY",
        "-v": "bind mount a volume HOST:CONTAINER",
    },
    "ps": {
        "--all": "show all containers (default shows only running)",
        "--filter": "filter output based on conditions provided",
        "--format": "format output using a Go template",
        "--last": "show n last created containers",
        "--no-trunc": "don't truncate output",
        "--quiet": "only display container IDs",
        "--size": "display total file sizes",
        "-a": "show all containers",
        "-f": "filter output",
        "-n": "show n last created containers",
        "-q": "only display container IDs",
        "-s": "display total file sizes",
    },
    "build": {
        "--file": "name of the Dockerfile",
        "--no-cache": "do not use cache when building the image",
        "--platform": "set platform if server is multi-platform capable",
        "--pull": "always attempt to pull a newer version of the image",
        "--tag": "name and optionally a tag in the name:tag format",
        "-f": "name of the Dockerfile",
        "-t": "name and optionally a tag",
    },
    "logs": {
        "--follow": "follow log output",
        "--since": "show logs since timestamp",
        "--tail": "number of lines to show from end of logs",
        "--timestamps": "show timestamps",
        "--until": "show logs before a timestamp",
        "-f": "follow log output",
        "-n": "number of lines to show",
        "-t": "show timestamps",
    },
    "exec": {
        "--detach": "detached mode: run command in the background",
        "--env": "set environment variables",
        "--interactive": "keep STDIN open even if not attached",
        "--tty": "allocate a pseudo-TTY",
        "--user": "username or UID",
        "--workdir": "working directory inside the container",
        "-d": "run command in the background",
        "-e": "set environment variables",
        "-i": "keep STDIN open",
        "-t": "allocate a pseudo-TTY",
        "-u": "username or UID",
        "-w": "working directory inside the container",
    },
}


def _run_docker(args: list[str], timeout: float = 3.0) -> list[str]:
    try:
        result = subprocess.run(
            ["docker"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


class DockerContainerCompleter(Completer):
    def __init__(self, all_containers: bool = False):
        self._all = all_containers

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        args = ["ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"]
        if self._all:
            args.append("--all")
        lines = _run_docker(args)
        prefix = ctx.prefix
        completions = []
        for line in lines:
            parts = line.split("\t")
            name = parts[0] if parts else ""
            desc = f"{parts[1]} ({parts[2]})" if len(parts) >= 3 else ""
            if name.startswith(prefix):
                completions.append(Completion(value=name, description=desc))
        return completions


class DockerImageCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_docker(["images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"])
        prefix = ctx.prefix
        seen: set[str] = set()
        completions = []
        for line in lines:
            parts = line.split("\t")
            name = parts[0] if parts else ""
            desc = parts[1] if len(parts) >= 2 else ""
            if name.endswith(":<none>") or name == "<none>:<none>":
                continue
            if name not in seen and name.startswith(prefix):
                seen.add(name)
                completions.append(Completion(value=name, description=desc))
        return completions


class DockerSubcommandCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        return [
            Completion(value=sub, description=desc)
            for sub, desc in DOCKER_SUBCOMMANDS.items()
            if sub.startswith(prefix)
        ]


class DockerArgCompleter(Completer):
    """Dispatches completions based on the docker subcommand."""

    _running = DockerContainerCompleter(all_containers=False)
    _all_containers = DockerContainerCompleter(all_containers=True)
    _images = DockerImageCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        subcmd = ctx.args[0]

        if subcmd in ("exec", "logs", "kill", "stop", "pause", "unpause",
                      "restart", "top", "stats", "commit"):
            return self._running.complete(ctx)

        if subcmd in ("rm", "inspect", "start"):
            return self._all_containers.complete(ctx)

        if subcmd in ("rmi", "tag", "push"):
            return self._images.complete(ctx)

        if subcmd == "run":
            if ctx.arg_index == 1:
                return self._images.complete(ctx)

        return []


class DockerSubcommandOptionsCompleter(Completer):
    def should_activate(self, ctx: CompletionContext) -> bool:
        return ctx.prefix.startswith("-")

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        subcmd = ctx.args[0]
        options = _SUBCOMMAND_OPTIONS.get(subcmd)
        if not options:
            return []
        prefix = ctx.prefix
        return [
            Completion(value=flag, description=desc, multi_select=True)
            for flag, desc in sorted(options.items())
            if flag.startswith(prefix)
        ]


def register(registry: CommandRegistry) -> None:
    registry.register_external_completers("docker", {
        None: DockerSubcommandOptionsCompleter(),
        0: DockerSubcommandCompleter(),
        1: DockerArgCompleter(),
        2: DockerArgCompleter(),
        3: DockerArgCompleter(),
    })
