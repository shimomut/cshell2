"""Completion recipe for the AWS CLI, modelled as a sub-command tree.

Currently models the ``aws s3`` service group and its sub-commands:
ls, cp, mv, sync, rm, mb, rb, presign, website.

Global flags (``--region``, ``--profile``, …) are declared at the root and
inherit down to every leaf, so they can be typed at any position once the
defining ancestor is reached in the walk.
"""

from __future__ import annotations

import configparser
import os
import subprocess
from pathlib import Path

from ..commands import registry as command_registry, arg
from ..completion import Completer, Completion, CompletionContext, FileCompleter
from ..variables import EnvVar, Var, registry as var_registry

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

class AwsRegionCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=region, description=description)
            for region, description in AWS_REGIONS
            if region.startswith(ctx.prefix)
        ]


class AwsProfileCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        profiles = self._get_profiles()
        return [
            Completion(value=p, description="AWS profile")
            for p in profiles if p.startswith(ctx.prefix)
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
                if section.startswith("profile "):
                    profiles.add(section[len("profile "):])
                elif section.lower() != "default":
                    profiles.add(section)
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


class S3PathCompleter(Completer):
    """Completes S3 URIs (s3://bucket/key) and local filesystem paths."""

    _file = FileCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if ctx.prefix.startswith("s3://"):
            return self._complete_s3(ctx.prefix)
        return self._file.complete(ctx)

    def _complete_s3(self, prefix: str) -> list[Completion]:
        rest = prefix[5:]
        slash_pos = rest.find("/")
        if slash_pos == -1:
            return self._list_buckets(rest)
        bucket = rest[:slash_pos]
        key_part = rest[slash_pos + 1:]
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
                idx = line.index("PRE ") + 4
                name = line[idx:].strip()
                if name.startswith(partial):
                    completions.append(Completion(
                        value=parent_uri + name,
                        display=name,
                        description="prefix",
                    ))
            else:
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


# ─── Tree definition ─────────────────────────────────────────────────────────

# Shared option lists for transfer-style commands
_TRANSFER_OPTIONS = [
    arg("--acl",                metavar="ACL",          help="ACL (private|public-read|...)"),
    arg("--cache-control",      metavar="VALUE",        help="Cache-Control header"),
    arg("--content-disposition", metavar="VALUE",       help="Content-Disposition header"),
    arg("--content-encoding",   metavar="VALUE",        help="Content-Encoding header"),
    arg("--content-language",   metavar="VALUE",        help="Content-Language header"),
    arg("--content-type",       metavar="VALUE",        help="Content-Type header"),
    arg("--dryrun",             action="store_true",    help="display operations without executing"),
    arg("--exclude",            metavar="PATTERN",      help="exclude matching files"),
    arg("--follow-symlinks",    action="store_true",    help="follow symbolic links"),
    arg("--include",            metavar="PATTERN",      help="include matching files"),
    arg("--metadata",           metavar="KEY=VAL",      help="metadata pairs"),
    arg("--metadata-directive", metavar="DIRECTIVE",    help="copy or replace metadata"),
    arg("--no-follow-symlinks", action="store_true",    help="do not follow symlinks"),
    arg("--no-guess-mime-type", action="store_true",    help="do not guess MIME type"),
    arg("--no-progress",        action="store_true",    help="hide progress bar"),
    arg("--only-show-errors",   action="store_true",    help="only show errors"),
    arg("--quiet",              action="store_true",    help="suppress all output"),
    arg("--recursive",          action="store_true",    help="recurse into directories"),
    arg("--request-payer",      metavar="REQUESTER",    help="confirm requester pays"),
    arg("--source-region",      metavar="REGION",       help="source bucket region"),
    arg("--sse",                metavar="ALGO",         help="server-side encryption (AES256|aws:kms)"),
    arg("--sse-kms-key-id",     metavar="KEY_ID",       help="customer master key ID"),
    arg("--storage-class",      metavar="CLASS",        help="storage class"),
]


def register() -> None:
    aws = command_registry.command(
        "aws", help="AWS CLI",
        params=[
            arg("--color",        metavar="WHEN",     help="color output (on|off|auto)"),
            arg("--debug",        action="store_true", help="turn on debug logging"),
            arg("--endpoint-url", metavar="URL",      help="override endpoint URL"),
            arg("--no-cli-pager", action="store_true", help="disable pager"),
            arg("--no-color",     action="store_true", help="disable color output"),
            arg("--no-verify-ssl", action="store_true", help="disable SSL verification"),
            arg("--output",       metavar="FORMAT",   help="output format (json|text|table|yaml)"),
            arg("--profile",      metavar="PROFILE",  help="named profile",
                                  completer=AwsProfileCompleter()),
            arg("--region",       metavar="REGION",   help="override endpoint region",
                                  completer=AwsRegionCompleter()),
        ],
    )

    s3 = aws.command("s3", help="Amazon S3 — object storage")

    s3.command(
        "ls", help="list S3 objects under a bucket or prefix",
        params=[
            arg("path", nargs="?", completer=S3PathCompleter()),
            arg("--human-readable", action="store_true", help="human readable sizes"),
            arg("--page-size",      metavar="N", type=int, help="API page size"),
            arg("--recursive",      action="store_true", help="recurse"),
            arg("--request-payer",  metavar="REQUESTER", help="requester pays"),
            arg("--summarize",      action="store_true", help="show summary"),
        ],
    )

    s3.command(
        "cp", help="copy a local file or S3 object to another location",
        params=[
            arg("src", completer=S3PathCompleter()),
            arg("dst", completer=S3PathCompleter()),
            *_TRANSFER_OPTIONS,
            arg("--expected-size",    metavar="BYTES", help="expected file size"),
            arg("--grants",           metavar="GRANT", help="grant permissions"),
            arg("--website-redirect", metavar="URL",   help="website redirect"),
        ],
    )

    s3.command(
        "mv", help="move a local file or S3 object to another location",
        params=[
            arg("src", completer=S3PathCompleter()),
            arg("dst", completer=S3PathCompleter()),
            *_TRANSFER_OPTIONS,
            arg("--grants",           metavar="GRANT", help="grant permissions"),
            arg("--website-redirect", metavar="URL",   help="website redirect"),
        ],
    )

    s3.command(
        "sync", help="sync directories and S3 prefixes",
        params=[
            arg("src", completer=S3PathCompleter()),
            arg("dst", completer=S3PathCompleter()),
            *_TRANSFER_OPTIONS,
            arg("--delete",            action="store_true", help="delete extras at dst"),
            arg("--exact-timestamps",  action="store_true", help="exact timestamp match"),
            arg("--grants",            metavar="GRANT",     help="grant permissions"),
            arg("--size-only",         action="store_true", help="compare by size only"),
        ],
    )

    s3.command(
        "rm", help="delete an S3 object",
        params=[
            arg("path", completer=S3PathCompleter()),
            arg("--dryrun",          action="store_true", help="dry run"),
            arg("--exclude",         metavar="PATTERN",   help="exclude pattern"),
            arg("--include",         metavar="PATTERN",   help="include pattern"),
            arg("--no-progress",     action="store_true", help="hide progress"),
            arg("--only-show-errors", action="store_true", help="only show errors"),
            arg("--page-size",       metavar="N", type=int, help="API page size"),
            arg("--quiet",           action="store_true", help="suppress output"),
            arg("--recursive",       action="store_true", help="delete recursively"),
            arg("--request-payer",   metavar="REQUESTER", help="requester pays"),
        ],
    )

    s3.command(
        "mb", help="make a new S3 bucket",
        params=[arg("path", completer=S3PathCompleter())],
    )

    s3.command(
        "rb", help="remove an empty S3 bucket",
        params=[
            arg("path", completer=S3PathCompleter()),
            arg("--force", action="store_true", help="remove contents first"),
        ],
    )

    s3.command(
        "presign", help="generate a pre-signed URL for an S3 object",
        params=[
            arg("path", completer=S3PathCompleter()),
            arg("--expires-in", metavar="SECONDS", type=int, help="expiry seconds"),
        ],
    )

    s3.command(
        "website", help="set the website configuration for a bucket",
        params=[
            arg("path", completer=S3PathCompleter()),
            arg("--index-document", metavar="KEY", help="index document"),
            arg("--error-document", metavar="KEY", help="error document"),
        ],
    )

    # ─── Variables ───────────────────────────────────────────────────────
    var_registry.register(_AwsRegionVar())
    var_registry.register(EnvVar(
        name="aws_profile",
        env_var="AWS_PROFILE",
        completer=AwsProfileCompleter(),
        description="AWS named profile",
    ))


class _AwsRegionVar(Var):
    """Sets AWS_REGION and AWS_DEFAULT_REGION together from one logical name."""

    @property
    def name(self) -> str:
        return "aws_region"

    @property
    def description(self) -> str:
        return "AWS region — sets AWS_REGION + AWS_DEFAULT_REGION"

    @property
    def env_keys(self) -> list[str]:
        return ["AWS_REGION", "AWS_DEFAULT_REGION"]

    def get(self) -> str | None:
        return os.environ.get("AWS_REGION")

    def set(self, value: str) -> None:
        os.environ["AWS_REGION"] = value
        os.environ["AWS_DEFAULT_REGION"] = value

    def unset(self) -> None:
        os.environ.pop("AWS_REGION", None)
        os.environ.pop("AWS_DEFAULT_REGION", None)

    @property
    def value_completer(self) -> Completer:
        return AwsRegionCompleter()
