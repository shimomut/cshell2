"""Completion recipe for aws (AWS CLI).

Currently supports the ``aws s3`` service group and its subcommands:
ls, cp, mv, sync, rm, mb, rb, presign, website.

Global flags ``--region`` and ``--profile`` are completed anywhere on the
command line, including when they appear before the service name.

Enable in ~/.cshell2/config.py::

    from cshell2.recipes import enable
    enable("aws")
"""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

from ..commands import CommandRegistry
from ..completion import Completer, Completion, CompletionContext, FileCompleter

# ─── Service map ─────────────────────────────────────────────────────────────

AWS_SERVICES: dict[str, str] = {
    "s3": "Amazon S3 — object storage",
    # More services can be added here as support grows
}

# ─── AWS regions ─────────────────────────────────────────────────────────────

AWS_REGIONS: list[tuple[str, str]] = [
    ("af-south-1",      "Africa (Cape Town)"),
    ("ap-east-1",       "Asia Pacific (Hong Kong)"),
    ("ap-northeast-1",  "Asia Pacific (Tokyo)"),
    ("ap-northeast-2",  "Asia Pacific (Seoul)"),
    ("ap-northeast-3",  "Asia Pacific (Osaka)"),
    ("ap-south-1",      "Asia Pacific (Mumbai)"),
    ("ap-south-2",      "Asia Pacific (Hyderabad)"),
    ("ap-southeast-1",  "Asia Pacific (Singapore)"),
    ("ap-southeast-2",  "Asia Pacific (Sydney)"),
    ("ap-southeast-3",  "Asia Pacific (Jakarta)"),
    ("ap-southeast-4",  "Asia Pacific (Melbourne)"),
    ("ap-southeast-5",  "Asia Pacific (Malaysia)"),
    ("ca-central-1",    "Canada (Central)"),
    ("ca-west-1",       "Canada West (Calgary)"),
    ("eu-central-1",    "Europe (Frankfurt)"),
    ("eu-central-2",    "Europe (Zurich)"),
    ("eu-north-1",      "Europe (Stockholm)"),
    ("eu-south-1",      "Europe (Milan)"),
    ("eu-south-2",      "Europe (Spain)"),
    ("eu-west-1",       "Europe (Ireland)"),
    ("eu-west-2",       "Europe (London)"),
    ("eu-west-3",       "Europe (Paris)"),
    ("il-central-1",    "Israel (Tel Aviv)"),
    ("me-central-1",    "Middle East (UAE)"),
    ("me-south-1",      "Middle East (Bahrain)"),
    ("mx-central-1",    "Mexico (Central)"),
    ("sa-east-1",       "South America (São Paulo)"),
    ("us-east-1",       "US East (N. Virginia)"),
    ("us-east-2",       "US East (Ohio)"),
    ("us-gov-east-1",   "AWS GovCloud (US-East)"),
    ("us-gov-west-1",   "AWS GovCloud (US-West)"),
    ("us-west-1",       "US West (N. California)"),
    ("us-west-2",       "US West (Oregon)"),
]

# ─── aws s3 subcommands ──────────────────────────────────────────────────────

S3_SUBCOMMANDS: dict[str, str] = {
    "cp":      "copy a local file or S3 object to another location",
    "ls":      "list S3 objects and common prefixes under a bucket or prefix",
    "mb":      "make a new S3 bucket",
    "mv":      "move a local file or S3 object to another location",
    "presign": "generate a pre-signed URL for an Amazon S3 object",
    "rb":      "remove an empty S3 bucket",
    "rm":      "delete an S3 object",
    "sync":    "sync directories and S3 prefixes",
    "website": "set the website configuration for a bucket",
}

# ─── Options ─────────────────────────────────────────────────────────────────

_GLOBAL_OPTIONS: dict[str, str] = {
    "--color":        "turn on/off color output (on|off|auto)",
    "--debug":        "turn on debug logging",
    "--endpoint-url": "override the command's default URL",
    "--no-cli-pager": "disable output through a pager",
    "--no-color":     "disable color output",
    "--no-verify-ssl":"override default behavior of verifying SSL certificates",
    "--output":       "output format (json|text|table|yaml)",
    "--profile":      "use a specific profile from your credential file",
    "--region":       "override the endpoint region",
}

# Flags that take a following value argument (used to mark arg_hint and to
# detect when to complete the value rather than the next positional arg).
_GLOBAL_VALUE_FLAGS: set[str] = {
    "--color", "--endpoint-url", "--output", "--profile", "--region",
}

# Options shared by cp, mv, sync (transfer commands)
_S3_TRANSFER_OPTIONS: dict[str, str] = {
    "--acl":                "sets the ACL for the object (private|public-read|...)",
    "--cache-control":      "sets the Cache-Control HTTP header",
    "--content-disposition":"sets the Content-Disposition HTTP header",
    "--content-encoding":   "sets the Content-Encoding HTTP header",
    "--content-language":   "sets the Content-Language HTTP header",
    "--content-type":       "sets the Content-Type HTTP header",
    "--dryrun":             "display operations without executing them",
    "--exclude":            "exclude files or objects matching the specified pattern",
    "--follow-symlinks":    "follow symbolic links when uploading to S3",
    "--include":            "include only files or objects matching the specified pattern",
    "--metadata":           "metadata to set on the object (key=value pairs)",
    "--metadata-directive": "specifies whether metadata is copied or replaced",
    "--no-follow-symlinks": "do not follow symbolic links",
    "--no-guess-mime-type": "do not guess MIME type based on file extension",
    "--no-progress":        "do not display a progress bar",
    "--only-show-errors":   "only show errors and warnings",
    "--quiet":              "suppress all output",
    "--recursive":          "perform the command recursively",
    "--request-payer":      "confirms that requester will pay (requester)",
    "--source-region":      "region of the source bucket",
    "--sse":                "server-side encryption algorithm (AES256|aws:kms)",
    "--sse-kms-key-id":     "customer master key ID for server-side encryption",
    "--storage-class":      "storage class (STANDARD|REDUCED_REDUNDANCY|STANDARD_IA|...)",
}

_S3_SUBCOMMAND_OPTIONS: dict[str, dict[str, str]] = {
    "ls": {
        "--human-readable": "display file sizes in human readable format",
        "--page-size":      "number of items to return per API call",
        "--recursive":      "perform the command recursively on all S3 objects",
        "--request-payer":  "confirms that requester will pay (requester)",
        "--summarize":      "display summary information (total objects and total size)",
    },
    "cp": {
        **_S3_TRANSFER_OPTIONS,
        "--expected-size":     "expected size of the file (for multipart progress tracking)",
        "--grants":            "grant permissions to individual users or groups",
        "--website-redirect":  "redirect requests to another object or external URL",
    },
    "mv": {
        **_S3_TRANSFER_OPTIONS,
        "--grants":           "grant permissions to individual users or groups",
        "--website-redirect": "redirect requests to another object or external URL",
    },
    "sync": {
        **_S3_TRANSFER_OPTIONS,
        "--delete":            "delete files in destination not present in source",
        "--exact-timestamps":  "sync only when timestamps match exactly",
        "--grants":            "grant permissions to individual users or groups",
        "--size-only":         "compare only object sizes, not timestamps",
    },
    "rm": {
        "--dryrun":          "display operations without executing them",
        "--exclude":         "exclude files or objects matching the specified pattern",
        "--include":         "include only files or objects matching the specified pattern",
        "--no-progress":     "do not display a progress bar",
        "--only-show-errors":"only show errors and warnings",
        "--page-size":       "number of items to return per API call",
        "--quiet":           "suppress all output",
        "--recursive":       "recursively delete all objects under the given prefix",
        "--request-payer":   "confirms that requester will pay (requester)",
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
        "--error-document":  "key name prefix for 4XX class error documents",
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


def _positional_args(args: list[str]) -> list[str]:
    """Return positional (non-flag, non-flag-value) args from *args*.

    Flags in ``_GLOBAL_VALUE_FLAGS`` consume the next token as their value;
    all other ``--foo`` / ``-f`` tokens are skipped as boolean flags.
    The remaining tokens (the actual positional arguments) are returned.
    """
    result: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _GLOBAL_VALUE_FLAGS:
            skip_next = True   # next token is the flag's value, skip it too
        elif arg.startswith("-"):
            pass               # boolean flag — skip
        else:
            result.append(arg)
    return result


# ─── Completers ──────────────────────────────────────────────────────────────

class AwsRegionCompleter(Completer):
    """Completes AWS region identifiers."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        return [
            Completion(value=region, description=description)
            for region, description in AWS_REGIONS
            if region.startswith(prefix)
        ]


class AwsProfileCompleter(Completer):
    """Completes AWS profile names from ~/.aws/config and ~/.aws/credentials."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        profiles = self._get_profiles()
        prefix = ctx.prefix
        return [
            Completion(value=p, description="AWS profile")
            for p in profiles
            if p.startswith(prefix)
        ]

    def _get_profiles(self) -> list[str]:
        profiles: set[str] = set()
        for config_file in (
            Path.home() / ".aws" / "credentials",
            Path.home() / ".aws" / "config",
        ):
            if not config_file.exists():
                continue
            parser = configparser.ConfigParser()
            try:
                parser.read(config_file)
            except configparser.Error:
                continue
            for section in parser.sections():
                # ~/.aws/config uses "profile NAME"; credentials uses "NAME"
                if section.startswith("profile "):
                    profiles.add(section[len("profile "):])
                elif section.lower() != "default":
                    profiles.add(section)
        # Always include "default" if it exists in either file
        for config_file in (
            Path.home() / ".aws" / "credentials",
            Path.home() / ".aws" / "config",
        ):
            if config_file.exists():
                parser = configparser.ConfigParser()
                try:
                    parser.read(config_file)
                    if "default" in parser:
                        profiles.add("default")
                except configparser.Error:
                    pass
        return sorted(profiles)


# Maps a value-taking global flag to its value completer.
_FLAG_VALUE_COMPLETERS: dict[str, Completer] = {
    "--region":  AwsRegionCompleter(),
    "--profile": AwsProfileCompleter(),
}


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


class AwsArgCompleter(Completer):
    """Dispatches positional completions for the AWS CLI.

    Handles global value-taking flags (``--region``, ``--profile``, …) that
    may appear *before* the service name by stripping flag+value pairs from
    ``ctx.args`` before doing positional dispatch.  Also completes the value
    of those flags when the previous token is the flag itself.
    """

    _s3_path = S3PathCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # Priority 1: complete the *value* of a value-taking flag.
        if ctx.args and ctx.args[-1] in _FLAG_VALUE_COMPLETERS:
            return _FLAG_VALUE_COMPLETERS[ctx.args[-1]].complete(ctx)

        # Strip flag+value pairs to get the "logical" positional args.
        positionals = _positional_args(ctx.args)
        effective_index = len(positionals)

        if effective_index == 0:
            # Complete the service name (e.g. "s3")
            prefix = ctx.prefix
            return [
                Completion(value=svc, description=desc)
                for svc, desc in AWS_SERVICES.items()
                if svc.startswith(prefix)
            ]

        service = positionals[0]

        if service == "s3":
            return self._complete_s3(ctx, positionals, effective_index)

        return []

    def _complete_s3(
        self,
        ctx: CompletionContext,
        positionals: list[str],
        effective_index: int,
    ) -> list[Completion]:
        if effective_index == 1:
            # Complete the s3 subcommand name
            prefix = ctx.prefix
            return [
                Completion(value=sub, description=desc)
                for sub, desc in S3_SUBCOMMANDS.items()
                if sub.startswith(prefix)
            ]

        if len(positionals) < 2:
            return []
        subcmd = positionals[1]

        # effective_index >= 2: complete path arguments (S3 URI or local path)
        if subcmd in _S3_ONLY_SUBCMDS or subcmd in _S3_MIXED_SUBCMDS:
            return self._s3_path.complete(ctx)

        return []


class AwsOptionsCompleter(Completer):
    """Completes AWS CLI flags, merging global and per-subcommand options."""

    def should_activate(self, ctx: CompletionContext) -> bool:
        return ctx.prefix.startswith("-")

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        all_options: dict[str, str] = dict(_GLOBAL_OPTIONS)

        # Add service + subcommand specific options when we know them.
        # Strip flag+value pairs to find the logical positional args.
        positionals = _positional_args(ctx.args)
        if len(positionals) >= 2 and positionals[0] == "s3":
            subcmd = positionals[1]
            all_options.update(_S3_SUBCOMMAND_OPTIONS.get(subcmd, {}))

        prefix = ctx.prefix
        return [
            Completion(
                value=flag,
                description=desc,
                multi_select=True,
                arg_hint="VALUE" if flag in _GLOBAL_VALUE_FLAGS else "",
            )
            for flag, desc in sorted(all_options.items())
            if flag.startswith(prefix)
        ]


def register(registry: CommandRegistry) -> None:
    # Register AwsArgCompleter at enough positions to handle several global
    # flag+value pairs before the service name (each pair uses 2 slots).
    # Positions 0–7 covers up to 4 global flag+value pairs before "s3".
    arg_completer = AwsArgCompleter()
    registry.register_external_completers("aws", {
        None: AwsOptionsCompleter(),
        **{i: arg_completer for i in range(8)},
    })
