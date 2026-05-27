"""Completion recipe for docker.

Supports both legacy flat commands (docker run, docker ps) and management
group commands (docker image ls, docker container stop).
"""

from __future__ import annotations

import subprocess

from ..commands import registry as command_registry
from ..completion import Completer, Completion, CompletionContext, OptionsCompleter

# Management command groups (docker <group> <subcmd>)
_MANAGEMENT_GROUPS: frozenset[str] = frozenset({"container", "image", "network", "system", "volume"})

DOCKER_SUBCOMMANDS: dict[str, str] = {
    # Management groups
    "container": "manage containers",
    "image": "manage images",
    "network": "manage networks",
    "system": "manage Docker",
    "volume": "manage volumes",
    # Legacy flat commands
    "build": "build an image from a Dockerfile",
    "commit": "create a new image from a container's changes",
    "cp": "copy files between a container and the local filesystem",
    "create": "create a new container",
    "exec": "run a command in a running container",
    "images": "list images",
    "inspect": "return low-level information on containers/images/volumes",
    "kill": "kill one or more running containers",
    "logs": "fetch the logs of a container",
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
}

DOCKER_IMAGE_SUBCOMMANDS: dict[str, str] = {
    "build": "build an image from a Dockerfile",
    "history": "show the history of an image",
    "import": "import contents from a tarball to create a filesystem image",
    "inspect": "display detailed information on one or more images",
    "load": "load an image from a tar archive or STDIN",
    "ls": "list images",
    "prune": "remove unused images",
    "pull": "download an image from a registry",
    "push": "upload an image to a registry",
    "rm": "remove one or more images",
    "save": "save one or more images to a tar archive",
    "tag": "create a tag TARGET_IMAGE that refers to SOURCE_IMAGE",
}

DOCKER_CONTAINER_SUBCOMMANDS: dict[str, str] = {
    "attach": "attach local stdin/stdout/stderr to a running container",
    "commit": "create a new image from a container's changes",
    "cp": "copy files between container and local filesystem",
    "create": "create a new container",
    "diff": "inspect changes to files on a container's filesystem",
    "exec": "run a command in a running container",
    "export": "export a container's filesystem as a tar archive",
    "inspect": "display detailed information on one or more containers",
    "kill": "kill one or more running containers",
    "logs": "fetch the logs of a container",
    "ls": "list containers",
    "pause": "pause all processes within one or more containers",
    "port": "list port mappings for the container",
    "prune": "remove all stopped containers",
    "rename": "rename a container",
    "restart": "restart one or more containers",
    "rm": "remove one or more containers",
    "run": "run a command in a new container",
    "start": "start one or more stopped containers",
    "stats": "display live stream of container resource usage statistics",
    "stop": "stop one or more running containers",
    "top": "display the running processes of a container",
    "unpause": "unpause all processes within one or more containers",
    "update": "update configuration of one or more containers",
    "wait": "block until one or more containers stop, print exit codes",
}

_DOCKER_NETWORK_SUBCOMMANDS: dict[str, str] = {
    "connect": "connect a container to a network",
    "create": "create a network",
    "disconnect": "disconnect a container from a network",
    "inspect": "display detailed information on one or more networks",
    "ls": "list networks",
    "prune": "remove all unused networks",
    "rm": "remove one or more networks",
}

_DOCKER_VOLUME_SUBCOMMANDS: dict[str, str] = {
    "create": "create a volume",
    "inspect": "display detailed information on one or more volumes",
    "ls": "list volumes",
    "prune": "remove all unused local volumes",
    "rm": "remove one or more volumes",
}

_DOCKER_SYSTEM_SUBCOMMANDS: dict[str, str] = {
    "df": "show Docker disk usage",
    "events": "get real-time events from the server",
    "info": "display system-wide information",
    "prune": "remove unused data",
}

_GROUP_SUBCOMMANDS: dict[str, dict[str, str]] = {
    "image": DOCKER_IMAGE_SUBCOMMANDS,
    "container": DOCKER_CONTAINER_SUBCOMMANDS,
    "network": _DOCKER_NETWORK_SUBCOMMANDS,
    "volume": _DOCKER_VOLUME_SUBCOMMANDS,
    "system": _DOCKER_SYSTEM_SUBCOMMANDS,
}

# Options keyed by flat subcommand name or (group, subcommand) tuple.
_SUBCOMMAND_OPTIONS: dict[str | tuple[str, str], dict[str, str]] = {
    # --- flat legacy commands ---
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
    # --- docker image <subcmd> ---
    ("image", "ls"): {
        "--all": "show all images (including intermediate)",
        "--digests": "show digests",
        "--filter": "filter output based on conditions provided",
        "--format": "format output using a Go template",
        "--no-trunc": "don't truncate output",
        "--quiet": "only show image IDs",
        "-a": "show all images",
        "-f": "filter output",
        "-q": "only show image IDs",
    },
    ("image", "rm"): {
        "--force": "force removal of the image",
        "--no-prune": "do not delete untagged parent images",
        "-f": "force removal of the image",
    },
    ("image", "build"): {
        "--file": "name of the Dockerfile",
        "--no-cache": "do not use cache when building the image",
        "--platform": "set platform if server is multi-platform capable",
        "--pull": "always attempt to pull a newer version of the image",
        "--tag": "name and optionally a tag in the name:tag format",
        "-f": "name of the Dockerfile",
        "-t": "name and optionally a tag",
    },
    ("image", "pull"): {
        "--all-tags": "download all tagged images in the repository",
        "--disable-content-trust": "skip image verification",
        "--platform": "set platform if server is multi-platform capable",
        "--quiet": "suppress verbose output",
        "-a": "download all tagged images",
        "-q": "suppress verbose output",
    },
    ("image", "push"): {
        "--all-tags": "push all tagged images in the repository",
        "--disable-content-trust": "skip image signing",
        "--quiet": "suppress verbose output",
        "-a": "push all tagged images",
        "-q": "suppress verbose output",
    },
    ("image", "prune"): {
        "--all": "remove all unused images, not just dangling ones",
        "--filter": "provide filter values",
        "--force": "do not prompt for confirmation",
        "-a": "remove all unused images",
        "-f": "do not prompt for confirmation",
    },
    # --- docker container <subcmd> ---
    ("container", "ls"): {
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
    ("container", "rm"): {
        "--force": "force removal of a running container",
        "--link": "remove the specified link",
        "--volumes": "remove anonymous volumes associated with the container",
        "-f": "force removal of a running container",
        "-l": "remove the specified link",
        "-v": "remove anonymous volumes",
    },
    ("container", "run"): {
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
    ("container", "exec"): {
        "--detach": "run command in the background",
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
    ("container", "logs"): {
        "--follow": "follow log output",
        "--since": "show logs since timestamp",
        "--tail": "number of lines to show from end of logs",
        "--timestamps": "show timestamps",
        "--until": "show logs before a timestamp",
        "-f": "follow log output",
        "-n": "number of lines to show",
        "-t": "show timestamps",
    },
    ("container", "stop"): {
        "--time": "seconds to wait for stop before killing",
        "-t": "seconds to wait for stop before killing",
    },
    ("container", "prune"): {
        "--filter": "provide filter values",
        "--force": "do not prompt for confirmation",
        "-f": "do not prompt for confirmation",
    },
    ("container", "start"): {
        "--attach": "attach STDOUT/STDERR and forward signals",
        "--interactive": "attach container's STDIN",
        "-a": "attach STDOUT/STDERR",
        "-i": "attach container's STDIN",
    },
}

# Which container subcommands take running vs. all containers as args
_CONTAINER_TAKES_RUNNING = frozenset({
    "exec", "logs", "kill", "stop", "pause", "unpause",
    "restart", "top", "stats", "commit", "attach", "diff",
    "export", "port", "wait",
})
_CONTAINER_TAKES_ALL = frozenset({
    "rm", "inspect", "start", "rename", "update",
})
# Which image subcommands take image names as args
_IMAGE_TAKES_IMAGE = frozenset({
    "rm", "inspect", "tag", "push", "history", "save",
})


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


_running_containers = DockerContainerCompleter(all_containers=False)
_all_containers = DockerContainerCompleter(all_containers=True)
_images = DockerImageCompleter()


class DockerSubcommandCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        return [
            Completion(value=sub, description=desc)
            for sub, desc in DOCKER_SUBCOMMANDS.items()
            if sub.startswith(prefix)
        ]


class DockerArgCompleter(Completer):
    """Dispatches completions based on the docker subcommand or management group."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        first = ctx.args[0]

        # Management group: docker <group> <subcmd> [<arg>...]
        if first in _MANAGEMENT_GROUPS:
            return self._complete_group(first, ctx)

        # Legacy flat command: docker <subcmd> [<arg>...]
        return self._complete_flat(first, ctx)

    def _complete_group(self, group: str, ctx: CompletionContext) -> list[Completion]:
        subcommands = _GROUP_SUBCOMMANDS.get(group, {})

        if ctx.arg_index == 1:
            # Complete the sub-subcommand name
            prefix = ctx.prefix
            return [
                Completion(value=sub, description=desc)
                for sub, desc in subcommands.items()
                if sub.startswith(prefix)
            ]

        # arg_index >= 2: complete positional args to the sub-subcommand
        if len(ctx.args) < 2:
            return []
        subcmd = ctx.args[1]

        if group == "image":
            if subcmd in _IMAGE_TAKES_IMAGE:
                return _images.complete(ctx)
            if subcmd == "run":
                return _images.complete(ctx)

        elif group == "container":
            if subcmd in _CONTAINER_TAKES_RUNNING:
                return _running_containers.complete(ctx)
            if subcmd in _CONTAINER_TAKES_ALL:
                return _all_containers.complete(ctx)
            if subcmd == "run" and ctx.arg_index == 2:
                return _images.complete(ctx)

        return []

    def _complete_flat(self, subcmd: str, ctx: CompletionContext) -> list[Completion]:
        if subcmd in ("exec", "logs", "kill", "stop", "pause", "unpause",
                      "restart", "top", "stats", "commit"):
            return _running_containers.complete(ctx)
        if subcmd in ("rm", "inspect", "start"):
            return _all_containers.complete(ctx)
        if subcmd in ("rmi", "tag", "push"):
            return _images.complete(ctx)
        if subcmd == "run" and ctx.arg_index == 1:
            return _images.complete(ctx)
        return []


class DockerSubcommandOptionsCompleter(Completer):
    def should_activate(self, ctx: CompletionContext) -> bool:
        return ctx.prefix.startswith("-")

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        first = ctx.args[0]

        # Management group: look up by (group, subcmd) tuple
        if first in _MANAGEMENT_GROUPS:
            if len(ctx.args) < 2:
                return []
            key: str | tuple[str, str] = (first, ctx.args[1])
        else:
            key = first

        options = _SUBCOMMAND_OPTIONS.get(key)
        if not options:
            return []
        prefix = ctx.prefix
        return [
            Completion(value=flag, description=desc, multi_select=True)
            for flag, desc in sorted(options.items())
            if flag.startswith(prefix)
        ]


def register() -> None:
    command_registry.register_external_completers("docker", {
        None: DockerSubcommandOptionsCompleter(),
        0: DockerSubcommandCompleter(),
        1: DockerArgCompleter(),
        2: DockerArgCompleter(),
        3: DockerArgCompleter(),
    })
