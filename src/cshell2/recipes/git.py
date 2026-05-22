"""Completion recipe for git."""

from __future__ import annotations

import subprocess

from ..commands import CommandRegistry
from ..completion import Completer, Completion, CompletionContext, FileCompleter, OptionsCompleter

GIT_SUBCOMMANDS: dict[str, str] = {
    "add": "add file contents to the index",
    "bisect": "use binary search to find the commit that introduced a bug",
    "blame": "show what revision and author last modified each line of a file",
    "branch": "list, create, or delete branches",
    "checkout": "switch branches or restore working tree files",
    "cherry-pick": "apply the changes introduced by some existing commits",
    "clean": "remove untracked files from the working tree",
    "clone": "clone a repository into a new directory",
    "commit": "record changes to the repository",
    "diff": "show changes between commits or working tree",
    "fetch": "download objects and refs from another repository",
    "grep": "print lines matching a pattern",
    "init": "create an empty Git repository or reinitialize an existing one",
    "log": "show the commit logs",
    "merge": "join two or more development histories together",
    "mv": "move or rename a file, a directory, or a symlink",
    "pull": "fetch from and integrate with another repository or a local branch",
    "push": "update remote refs along with associated objects",
    "rebase": "reapply commits on top of another base tip",
    "remote": "manage set of tracked repositories",
    "reset": "reset current HEAD to the specified state",
    "restore": "restore working tree files",
    "revert": "revert some existing commits",
    "rm": "remove files from the working tree and from the index",
    "show": "show various types of objects",
    "stash": "stash the changes in a dirty working directory away",
    "status": "show the working tree status",
    "switch": "switch branches",
    "tag": "create, list, delete or verify a tag object",
}

GIT_STASH_SUBCOMMANDS: dict[str, str] = {
    "apply": "apply a stash entry without removing it",
    "clear": "remove all stash entries",
    "drop": "remove a single stash entry",
    "list": "list stash entries",
    "pop": "apply and remove the latest stash entry",
    "push": "save the current state to the stash",
    "show": "show the changes recorded in the stash entry",
}

_SUBCOMMAND_OPTIONS: dict[str, dict[str, str]] = {
    "commit": {
        "--all": "stage all tracked modified and deleted files",
        "--amend": "replace the tip of the current branch",
        "--dry-run": "show what would be committed",
        "--no-edit": "use the selected commit message without editing",
        "--verbose": "show unified diff between HEAD and what would be committed",
        "-a": "stage all tracked modified and deleted files",
        "-m": "use the given message as the commit message",
        "-v": "show unified diff",
    },
    "diff": {
        "--cached": "view staged changes (alias: --staged)",
        "--ignore-space-change": "ignore changes in whitespace amount",
        "--name-only": "show only names of changed files",
        "--name-status": "show names and status of changed files",
        "--staged": "view staged changes",
        "--stat": "show diffstat",
        "--word-diff": "show word-level diff",
        "-w": "ignore all whitespace",
    },
    "log": {
        "--all": "show all refs",
        "--follow": "follow file renames",
        "--graph": "draw a text-based graphical representation",
        "--no-merges": "do not print commits with more than one parent",
        "--oneline": "shorthand for --pretty=oneline --abbrev-commit",
        "--patch": "generate patch",
        "--reverse": "output commits in reverse order",
        "--stat": "show diffstat for each commit",
        "-n": "limit number of commits to output",
        "-p": "generate patch (alias for --patch)",
    },
    "push": {
        "--all": "push all branches",
        "--delete": "delete remote ref",
        "--dry-run": "do everything except actually send the updates",
        "--follow-tags": "push all local tags missing from remote",
        "--force": "force push (use with caution)",
        "--set-upstream": "set upstream tracking for the current branch",
        "--tags": "push all refs under refs/tags",
        "-f": "force push (use with caution)",
        "-u": "set upstream tracking (alias for --set-upstream)",
    },
    "checkout": {
        "-b": "create and switch to a new branch",
        "-B": "create/reset and switch to a branch",
        "--detach": "detach HEAD at the named commit",
        "--orphan": "create a new orphan branch",
        "--track": "set up tracking mode",
    },
    "reset": {
        "--soft": "reset HEAD only, keep index and working tree",
        "--mixed": "reset HEAD and index, keep working tree (default)",
        "--hard": "reset HEAD, index, and working tree",
    },
    "rebase": {
        "--abort": "abort the current rebase operation",
        "--continue": "continue after resolving conflicts",
        "--interactive": "make a list of commits to rebase",
        "--skip": "skip the current patch",
        "-i": "make a list of commits to rebase (alias for --interactive)",
    },
}


def _run_git(args: list[str], timeout: float = 2.0) -> list[str]:
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


class GitBranchCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["branch", "--all", "--format=%(refname:short)"])
        prefix = ctx.prefix
        return [
            Completion(value=b, description="branch")
            for b in lines
            if b.startswith(prefix)
        ]


class GitTagCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["tag"])
        prefix = ctx.prefix
        return [Completion(value=t, description="tag") for t in lines if t.startswith(prefix)]


class GitModifiedFileCompleter(Completer):
    """Completes files that are modified, untracked, or staged."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["status", "--short"])
        prefix = ctx.prefix
        seen: set[str] = set()
        completions = []
        for line in lines:
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ")[-1]
            if path not in seen and path.startswith(prefix):
                seen.add(path)
                completions.append(Completion(value=path))
        return completions


class GitRemoteCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["remote"])
        prefix = ctx.prefix
        return [Completion(value=r, description="remote") for r in lines if r.startswith(prefix)]


class GitStashRefCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["stash", "list", "--format=%gd: %s"])
        prefix = ctx.prefix
        completions = []
        for line in lines:
            parts = line.split(": ", 1)
            ref = parts[0]
            desc = parts[1] if len(parts) > 1 else ""
            if ref.startswith(prefix):
                completions.append(Completion(value=ref, description=desc))
        return completions


class GitSubcommandCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        return [
            Completion(value=sub, description=desc)
            for sub, desc in GIT_SUBCOMMANDS.items()
            if sub.startswith(prefix)
        ]


class GitArgCompleter(Completer):
    """Dispatches to the appropriate completer based on the git subcommand."""

    _branch = GitBranchCompleter()
    _tag = GitTagCompleter()
    _file = FileCompleter()
    _modified = GitModifiedFileCompleter()
    _remote = GitRemoteCompleter()
    _stash_ref = GitStashRefCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        subcmd = ctx.args[0]

        if subcmd in ("checkout", "switch", "merge", "cherry-pick"):
            return self._branch.complete(ctx)

        if subcmd == "rebase":
            return self._branch.complete(ctx)

        if subcmd in ("push", "fetch"):
            if ctx.arg_index == 1:
                return self._remote.complete(ctx)
            return self._branch.complete(ctx)

        if subcmd == "pull":
            if ctx.arg_index == 1:
                return self._remote.complete(ctx)
            return self._branch.complete(ctx)

        if subcmd in ("add", "restore", "rm", "mv"):
            return self._modified.complete(ctx)

        if subcmd in ("diff", "show", "blame", "log"):
            results = self._branch.complete(ctx) + self._tag.complete(ctx) + self._file.complete(ctx)
            seen: set[str] = set()
            unique = []
            for c in results:
                if c.value not in seen:
                    seen.add(c.value)
                    unique.append(c)
            return unique

        if subcmd == "stash":
            if ctx.arg_index == 1:
                prefix = ctx.prefix
                return [
                    Completion(value=sub, description=desc)
                    for sub, desc in GIT_STASH_SUBCOMMANDS.items()
                    if sub.startswith(prefix)
                ]
            if len(ctx.args) > 1 and ctx.args[1] in ("apply", "drop", "show"):
                return self._stash_ref.complete(ctx)
            return []

        if subcmd == "tag":
            return self._tag.complete(ctx)

        if subcmd == "branch":
            return self._branch.complete(ctx)

        if subcmd == "remote":
            return self._remote.complete(ctx)

        if subcmd == "reset":
            return self._branch.complete(ctx) + self._tag.complete(ctx)

        return []


class GitSubcommandOptionsCompleter(Completer):
    """Options completer that dispatches by git subcommand."""

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
    registry.register_external_completers("git", {
        None: GitSubcommandOptionsCompleter(),
        0: GitSubcommandCompleter(),
        1: GitArgCompleter(),
        2: GitArgCompleter(),
        3: GitArgCompleter(),
    })
