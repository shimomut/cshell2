"""Completion recipe for aws (AWS CLI).

Currently supports the ``aws s3`` service group and its subcommands:
ls, cp, mv, sync, rm, mb, rb, presign, website.

Enable in ~/.cshell2/config.py::

    from cshell2.recipes import enable
    enable("aws")
"""

from __future__ import annotations

import subprocess

from ..commands import CommandRegistry
from ..completion import Completer, Completion, CompletionContext, FileCompleter

# ─── Service map ─────────────────────────────────────────────────────────────

AWS_SERVICES: dict[str, str] = {
    "s3": "Amazon S3 — object storage",
    # More services can be added here as support grows
}

# ─── aws s3 subcommands ──────────────────────────────────────────────────────

S3_SUBCOMMANDS: dict[str, str] = {
    "cp": "copy a local file or S3 object to another location",
    "ls": "list S3 objects and common prefixes under a bucket or prefix",
    "mb": "make a new S3 bucket",
    "mv": "move a local file or S3 object to another location",
    "presign": "generate a pre-signed URL for an Amazon S3 object",
    "rb": "remove an empty S3 bucket",
    "rm": "delete an S3 object",
    "sync": "sync directories and S3 prefixes",
    "website": "set the website configuration for a bucket",
}

# ─── Options ─────────────────────────────────────────────────────────────────

_GLOBAL_OPTIONS: dict[str, str] = {
    "--color": "turn on/off color output (on|off|auto)",
    "--debug": "turn on debug logging",
    "--endpoint-url": "override the command's default URL",
    "--no-cli-pager": "disable output through a pager",
    "--no-color": "disable color output",
    "--no-verify-ssl": "override default behavior of verifying SSL certificates",
    "--output": "output format (json|text|table|yaml)",
    "--profile": "use a specific profile from your credential file",
    "--region": "override the endpoint region",
}

# Options shared by cp, mv, sync (transfer commands)
_S3_TRANSFER_OPTIONS: dict[str, str] = {
    "--acl": "sets the ACL for the object (private|public-read|...)",
    "--cache-control": "sets the Cache-Control HTTP header",
    "--content-disposition": "sets the Content-Disposition HTTP header",
    "--content-encoding": "sets the Content-Encoding HTTP header",
    "--content-language": "sets the Content-Language HTTP header",
    "--content-type": "sets the Content-Type HTTP header",
    "--dryrun": "display operations without executing them",
    "--exclude": "exclude files or objects matching the specified pattern",
    "--follow-symlinks": "follow symbolic links when uploading to S3",
    "--include": "include only files or objects matching the specified pattern",
    "--metadata": "metadata to set on the object (key=value pairs)",
    "--metadata-directive": "specifies whether metadata is copied or replaced",
    "--no-follow-symlinks": "do not follow symbolic links",
    "--no-guess-mime-type": "do not guess MIME type based on file extension",
    "--no-progress": "do not display a progress bar",
    "--only-show-errors": "only show errors and warnings",
    "--quiet": "suppress all output",
    "--recursive": "perform the command recursively",
    "--request-payer": "confirms that requester will pay (requester)",
    "--source-region": "region of the source bucket",
    "--sse": "server-side encryption algorithm (AES256|aws:kms)",
    "--sse-kms-key-id": "customer master key ID for server-side encryption",
    "--storage-class": "storage class (STANDARD|REDUCED_REDUNDANCY|STANDARD_IA|...)",
}

_S3_SUBCOMMAND_OPTIONS: dict[str, dict[str, str]] = {
    "ls": {
        "--human-readable": "display file sizes in human readable format",
        "--page-size": "number of items to return per API call",
        "--recursive": "perform the command recursively on all S3 objects",
        "--request-payer": "confirms that requester will pay (requester)",
        "--summarize": "display summary information (total objects and total size)",
    },
    "cp": {
        **_S3_TRANSFER_OPTIONS,
        "--expected-size": "expected size of the file (for multipart progress tracking)",
        "--grants": "grant permissions to individual users or groups",
        "--website-redirect": "redirect requests to another object or external URL",
    },
    "mv": {
        **_S3_TRANSFER_OPTIONS,
        "--grants": "grant permissions to individual users or groups",
        "--website-redirect": "redirect requests to another object or external URL",
    },
    "sync": {
        **_S3_TRANSFER_OPTIONS,
        "--delete": "delete files in destination not present in source",
        "--exact-timestamps": "sync only when timestamps match exactly",
        "--grants": "grant permissions to individual users or groups",
        "--size-only": "compare only object sizes, not timestamps",
    },
    "rm": {
        "--dryrun": "display operations without executing them",
        "--exclude": "exclude files or objects matching the specified pattern",
        "--include": "include only files or objects matching the specified pattern",
        "--no-progress": "do not display a progress bar",
        "--only-show-errors": "only show errors and warnings",
        "--page-size": "number of items to return per API call",
        "--quiet": "suppress all output",
        "--recursive": "recursively delete all objects under the given prefix",
        "--request-payer": "confirms that requester will pay (requester)",
    },
    "mb": {
        "--region": "region in which to create the bucket",
    },
    "rb": {
        "--force": "remove bucket contents before deleting the bucket",
    },
    "presign": {
        "--expires-in": "seconds until the pre-signed URL expires (default 3600)",
    },
    "website": {
        "--index-document": "suffix appended to requests for a directory (e.g. index.html)",
        "--error-document": "key name prefix for 4XX class error documents",
    },
}

# s3 subcommands that accept S3 URIs only
_S3_ONLY_SUBCMDS: frozenset[str] = frozenset({"ls", "mb", "rb", "rm", "presign", "website"})
# s3 subcommands that accept both local paths and S3 URIs
_S3_MIXED_SUBCMDS: frozenset[str] = frozenset({"cp", "mv", "sync"})


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _run_aws(args: list[str], timeout: float = 5.0) -> list[str]:
    try:
        result = subprocess.run(
            ["aws"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


# ─── Completers ──────────────────────────────────────────────────────────────

class S3PathCompleter(Completer):
    """Completes S3 URIs (s3://bucket/key) and local filesystem paths.

    When the prefix starts with ``s3://``, calls ``aws s3 ls`` to enumerate
    buckets and object keys.  Otherwise delegates to :class:`FileCompleter`.
    """

    _file = FileCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if ctx.prefix.startswith("s3://"):
            return self._complete_s3(ctx.prefix)
        return self._file.complete(ctx)

    def _complete_s3(self, prefix: str) -> list[Completion]:
        rest = prefix[5:]  # strip leading "s3://"
        slash_pos = rest.find("/")

        if slash_pos == -1:
            # User is still typing the bucket name — list all buckets
            return self._list_buckets(rest)

        bucket = rest[:slash_pos]
        key_part = rest[slash_pos + 1:]  # everything after "s3://bucket/"

        # Find the "parent directory" to list, filter by what comes after it
        last_slash = key_part.rfind("/")
        if last_slash == -1:
            parent_uri = f"s3://{bucket}/"
            partial = key_part
        else:
            parent_key = key_part[: last_slash + 1]
            parent_uri = f"s3://{bucket}/{parent_key}"
            partial = key_part[last_slash + 1:]

        return self._list_objects(parent_uri, partial)

    def _list_buckets(self, partial: str) -> list[Completion]:
        lines = _run_aws(["s3", "ls"])
        completions = []
        for line in lines:
            # Format: "2023-01-01 00:00:00 bucket-name"
            parts = line.split()
            if len(parts) >= 3:
                bucket = parts[-1]
                if bucket.startswith(partial):
                    completions.append(Completion(
                        value=f"s3://{bucket}/",
                        display=f"{bucket}/",
                        description="bucket",
                    ))
        return completions

    def _list_objects(self, parent_uri: str, partial: str) -> list[Completion]:
        lines = _run_aws(["s3", "ls", parent_uri])
        completions = []
        for line in lines:
            if "PRE " in line:
                # Common prefix ("directory")
                # Format: "                           PRE subdir/"
                idx = line.index("PRE ") + 4
                name = line[idx:].strip()  # e.g. "subdir/"
                if name.startswith(partial):
                    completions.append(Completion(
                        value=parent_uri + name,
                        display=name,
                        description="prefix",
                    ))
            else:
                # Object line: "2023-01-01 00:00:00   12345 key with spaces.txt"
                # Split into at most 4 parts so the filename (which may contain
                # spaces) is captured whole in parts[3].
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    name = parts[3]
                    size = parts[2]
                    if name.startswith(partial):
                        completions.append(Completion(
                            value=parent_uri + name,
                            display=name,
                            description=f"{size} B",
                        ))
        return completions


class AwsServiceCompleter(Completer):
    """Completes the top-level AWS service name (e.g. s3)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        return [
            Completion(value=svc, description=desc)
            for svc, desc in AWS_SERVICES.items()
            if svc.startswith(prefix)
        ]


class AwsArgCompleter(Completer):
    """Dispatches completions based on the AWS service and subcommand."""

    _s3_path = S3PathCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        service = ctx.args[0]

        if service == "s3":
            return self._complete_s3(ctx)
        return []

    def _complete_s3(self, ctx: CompletionContext) -> list[Completion]:
        if ctx.arg_index == 1:
            # Complete the s3 subcommand name
            prefix = ctx.prefix
            return [
                Completion(value=sub, description=desc)
                for sub, desc in S3_SUBCOMMANDS.items()
                if sub.startswith(prefix)
            ]

        if len(ctx.args) < 2:
            return []
        subcmd = ctx.args[1]

        # arg_index >= 2: complete path arguments (S3 URI or local path)
        if subcmd in _S3_ONLY_SUBCMDS or subcmd in _S3_MIXED_SUBCMDS:
            return self._s3_path.complete(ctx)

        return []


class AwsOptionsCompleter(Completer):
    """Completes AWS CLI flags, merging global and per-subcommand options."""

    def should_activate(self, ctx: CompletionContext) -> bool:
        return ctx.prefix.startswith("-")

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        all_options: dict[str, str] = dict(_GLOBAL_OPTIONS)

        # Add service + subcommand specific options when we know them
        if len(ctx.args) >= 2 and ctx.args[0] == "s3":
            subcmd = ctx.args[1]
            all_options.update(_S3_SUBCOMMAND_OPTIONS.get(subcmd, {}))

        prefix = ctx.prefix
        return [
            Completion(value=flag, description=desc, multi_select=True)
            for flag, desc in sorted(all_options.items())
            if flag.startswith(prefix)
        ]


def register(registry: CommandRegistry) -> None:
    registry.register_external_completers("aws", {
        None: AwsOptionsCompleter(),
        0: AwsServiceCompleter(),
        1: AwsArgCompleter(),
        2: AwsArgCompleter(),
        3: AwsArgCompleter(),
    })
