"""Shared instruments for the ``awsut sagemaker`` tree.

Everything in here is presentation or plumbing that the ``jobs``, ``hub``,
``studio`` and ``hyperpod`` groups need.  It lives in one module so they cannot
drift in how they render the same data — several of these APIs hand back opaque
JSON documents, and all of them are read by following ARNs out of them.

The output contract itself — table shape, header line, note policy, ``error:``
lines — is one level up, in :mod:`cshell2.recipes._awsut_common`, because the
non-SageMaker half of ``awsut`` (``ec2``, ``logs``, ``cloudformation``) prints
to the same contract.  Those names are re-exported here, so a module in this
package still reads every instrument it needs off ``render``.

What this module adds on top is SageMaker-specific:

* **Documents are reformatted, never interpreted.**  ``JobConfigDocument`` and
  ``HubContentDocument`` are modelled by the service as opaque strings.
  Nothing here knows a single key name: structure is followed by *value
  shape* (an ARN says what it points at, an ``s3://`` URI says it is a
  location) and the JSON path a value was read from travels with it as
  provenance.
* **Clients, pagination and model introspection** for an API surface whose
  vintage varies with the installed botocore.

Region, profile, endpoint and service-name all come from the enclosing
``awsut`` recipe, i.e. from ``var aws_region=`` / ``var aws_profile=`` /
``var sagemaker_endpoint=`` / ``var sagemaker_service_name=`` at the prompt —
these commands deliberately have no ``--region`` / ``--profile`` flags.
"""

from __future__ import annotations

import functools
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import botocore.session

from .. import awsut
from .._awsut_common import (  # noqa: F401  (re-exported for this package)
    RED,
    RESET,
    YELLOW,
    SmError,
    api_message,
    error_code,
    fmt_bytes,
    fmt_dur,
    fmt_time,
    guard,
    print_header,
    print_labeled,
    print_table,
    section,
)

# Heartbeat cadence for the watch loops, independent of the poll interval: the
# dots exist to prove the loop is alive between polls, so they must not slow
# down when --interval grows.
DOT_INTERVAL = 5

# TAB candidates for the ``--instance-type`` flags.  A completion list, not a
# validation list — the service accepts far more than this, so nothing checks
# a typed value against it.  Shared because ``studio start`` and ``hyperpod``
# offer the same flag and should offer the same shortlist.
INSTANCE_TYPE_CHOICES = [
    "ml.trn1.32xlarge", "ml.p5.48xlarge", "ml.p5e.48xlarge",
    "ml.p5en.48xlarge", "ml.p4d.24xlarge", "ml.t3.xlarge",
    "ml.trn2.48xlarge", "ml.c4.large", "ml.c6i.large",
    "ml.t3.2xlarge", "ml.t3.large", "ml.c7g.medium",
]


# ─── AWS plumbing ───────────────────────────────────────────────────────────

def sm_client(region_name: str | None = None):
    """The SageMaker client, honouring the ``sagemaker_*`` Vars."""
    return awsut._get_sagemaker_client(region_name=region_name)


def s3_client():
    return awsut._get_boto3_client("s3")


def logs_client():
    return awsut._get_boto3_client("logs")


def region_label() -> str:
    return awsut._region_label()


NOT_FOUND_CODES = ("ValidationException", "ResourceNotFound",
                   "ResourceNotFoundException")


@functools.lru_cache(maxsize=None)
def model_enum(service_name: str, operation: str, member: str) -> tuple[str, ...]:
    """An input member's enum, read out of the loaded botocore model. Offline.

    Keyed by service name because ``var sagemaker_service_name=`` can point the
    client at a different (e.g. private) model, whose enums are its own.  A
    refreshed model contributes new values without a code change here.
    """
    try:
        model = botocore.session.Session().get_service_model(service_name)
        shape = model.operation_model(operation).input_shape.members[member]
        return tuple(getattr(shape, "enum", None) or ())
    except Exception:
        return ()


@functools.lru_cache(maxsize=None)
def input_members(service_name: str, operation: str) -> frozenset[str]:
    """An operation's input member names, from the loaded botocore model. Offline.

    botocore rejects a parameter its model does not declare *before* the call
    goes out, with a ``ParamValidationError`` that reads like a caller bug.  A
    command that sends an optional-but-recent member (``SpaceName`` on
    CreatePresignedDomainUrl) asks here first, so an old model produces an
    explanation instead.  Keyed by service name for the same reason
    :func:`model_enum` is: ``var sagemaker_service_name=`` can swap the model.
    """
    try:
        model = botocore.session.Session().get_service_model(service_name)
        return frozenset(model.operation_model(operation).input_shape.members)
    except Exception:
        return frozenset()


def require_input_member(operation: str, member: str, why: str) -> None:
    """Fail with an explanation when the loaded model's *operation* lacks *member*."""
    members = input_members(awsut.sagemaker_service_name, operation)
    if members and member not in members:
        raise SmError(
            f"the loaded model for service {awsut.sagemaker_service_name!r} has no "
            f"{member} input on {operation}, so {why} — upgrade botocore, or point "
            f"`var sagemaker_service_name=` at a model that has it"
        )


def require_operation(client, method: str, api: str) -> None:
    """Fail with an explanation when the loaded model lacks *api*.

    The Job APIs in particular are not in every botocore release, and a bare
    ``AttributeError`` on ``client.list_jobs`` would read like a cshell2 bug
    rather than a model-vintage problem.
    """
    if not hasattr(client, method):
        raise SmError(
            f"the loaded model for service {awsut.sagemaker_service_name!r} has no "
            f"{api} operation — upgrade botocore, or point "
            f"`var sagemaker_service_name=` at a model that has it"
        )


def paged(call, key, max_items, *, token_param="NextToken",
          token_key="NextToken", **params):
    """Collect up to *max_items* from a token-paginated list call.

    The token names are parameters because S3 does not use the SageMaker
    spelling: ``list_objects_v2`` sends ``ContinuationToken`` and returns
    ``NextContinuationToken``, so defaulting silently would stop after the
    first page and report a short listing as a complete one.
    """
    items = []
    token = None
    while len(items) < max_items:
        if token:
            params[token_param] = token
        resp = call(**params)
        items.extend(resp.get(key, []))
        token = resp.get(token_key)
        if not token:
            break
    return items[:max_items]


# ─── time / size formatting ─────────────────────────────────────────────────

def parse_since(text):
    """``'90m'`` / ``'24h'`` / ``'7d'`` → timezone-aware datetime, or None."""
    if not text:
        return None
    m = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not m:
        raise SmError(f"--since wants forms like 30m, 24h, 7d (got {text!r})")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
             "d": timedelta(days=n)}[unit]
    return datetime.now(timezone.utc) - delta


def age(dt):
    return (datetime.now(timezone.utc) - dt).total_seconds() if dt else None


def elapsed_of(started, ended):
    """Wall time between two API timestamps, or the age of *started*."""
    if started and ended:
        return (ended - started).total_seconds()
    return age(started)


# ─── opaque JSON documents ──────────────────────────────────────────────────

def parse_doc(doc):
    """A document string as parsed JSON, or None if it isn't parseable.

    The API models these documents as opaque strings, so they are plain JSON
    data with no schema this code can rely on: nothing here — or in the
    renderer below — knows a single key name, and a schema version never seen
    before renders the same way.
    """
    try:
        return json.loads(doc) if isinstance(doc, str) else doc
    except (TypeError, ValueError):
        return None


def yaml_scalar(value):
    """A leaf value as one unquoted token, quoted only where bareness misleads."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    text = str(value)
    # Quote only what would misread as structure: a value opening with '{' would
    # look like broken nesting, and ': ' like a key.  S3 URIs and ARNs contain
    # ':' but not ': ', so they stay bare and unwrapped — they are the copy
    # targets.
    if (not text or text.strip() != text or ": " in text
            or text[0] in "{[#&*!|>%@" or text.startswith("- ")):
        return json.dumps(text)
    return text


def yaml_lines(node, indent=0):
    """Parsed JSON as YAML-ish indented lines — a reading aid, purely structural.

    One-line JSON is unreadable at these depths, and the values worth reading
    (S3 URIs, ARNs) are exactly the ones a wrapped dump breaks.  So: nesting by
    indentation, one value per line, never truncated or wrapped.

    A string whose content is itself JSON is expanded rather than printed as an
    escaped blob — that is a shape check, not a key check, so it needs no schema
    knowledge.
    """
    pad = "  " * indent
    out = []

    if isinstance(node, dict):
        for key, value in node.items():
            nested = parse_doc(value) if _looks_like_json(value) else None
            if nested is not None:
                out.append(f"{pad}{key}:  # embedded JSON, expanded")
                out.extend(yaml_lines(nested, indent + 1))
            elif isinstance(value, (dict, list)) and value:
                out.append(f"{pad}{key}:")
                out.extend(yaml_lines(value, indent + 1))
            elif isinstance(value, str) and "\n" in value:
                out.append(f"{pad}{key}: |")
                out.extend(f"{pad}  {line}" for line in value.splitlines())
            else:
                out.append(f"{pad}{key}: {yaml_scalar(value)}")

    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)) and value:
                sub = yaml_lines(value, indent + 1)
                # Hang the item's first line off the dash so the entry reads as
                # one unit.
                out.append(f"{pad}- {sub[0].lstrip()}")
                out.extend(sub[1:])
            else:
                out.append(f"{pad}- {yaml_scalar(value)}")

    else:
        out.append(f"{pad}{yaml_scalar(node)}")

    return out


def _looks_like_json(value):
    return isinstance(value, str) and value.strip()[:1] in ("{", "[")


def show_document(label, doc, schema_version, raw=False, indent=0):
    """Print an opaque JSON-string document, reformatted (or verbatim)."""
    # Which of the two forms this is *does* vary per run (``--raw*`` picks), so
    # it belongs in the heading.  *Why* a reformat exists — the API models these
    # as one JSON string — does not, and lives in the flag's help instead.
    note = "verbatim" if raw else "reformatted"
    pad = "  " * indent
    print()
    if indent:
        # Nested inside ``trace``'s indented tree, where a section rule starting
        # at column zero would read as the end of the tree rather than a node
        # in it — so the label carries the caller's own padding instead.
        print(f"{label}  (schema {schema_version or '?'}; {note})")
    else:
        section(f"{label} · schema {schema_version or '?'} · {note}")
    if not doc:
        print(f"{pad}none returned")
        return
    if raw:
        for line in str(doc).splitlines():
            print(f"{pad}{line}")
        return
    parsed = parse_doc(doc)
    if parsed is None:
        print(f"{pad}not parseable JSON — verbatim:")
        for line in str(doc).splitlines():
            print(f"{pad}{line}")
        return
    for line in yaml_lines(parsed, indent):
        print(line)


# ─── references found by value shape ────────────────────────────────────────

def split_arn(value):
    """``('service', 'region', 'account', 'resource')`` for an ARN, else None.

    Structural only — the resource segment is what says which API can resolve
    it, so nothing here needs to know what any particular resource is called.
    """
    if not isinstance(value, str) or not value.startswith("arn:"):
        return None
    parts = value.split(":", 5)
    if len(parts) < 6:
        return None
    _arn, _partition, service, region, account, resource = parts
    return service, region, account, resource


def split_s3(uri):
    """``('bucket', 'key-prefix')`` for an ``s3://`` URI."""
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


def refs_in(node):
    """``[(json_path, value)]`` for every ARN and ``s3://`` URI in a document.

    Found by the shape of the value, not the name of the key: this code has no
    schema for these documents and must not pretend otherwise.  The JSON path
    travels along as provenance so every reported reference can be traced back
    to where it was read.
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str) and (node.startswith("arn:")
                                        or node.startswith("s3://")):
            found.append((path, node))

    walk(node, "")
    return found


def version_key(text):
    """Sort key for a hub content version: numeric where possible (0.0.10 > 0.0.9)."""
    parts = re.split(r"[.\-+]", str(text or ""))
    return tuple(int(p) if p.isdigit() else -1 for p in parts)


# ─── watch heartbeat ────────────────────────────────────────────────────────

class Heartbeat:
    """A run of ``.`` on one line meaning "polled, nothing changed".

    Any real output must call :meth:`clear` first, otherwise the change line
    lands mid-dots and the timeline becomes unreadable.
    """

    def __init__(self, width=60):
        self.col = 0
        self.width = width
        # Dot phase lives here, not in the sleep call, so the cadence is
        # wall-clock: restarting it per poll would let the poll's own duration
        # eat a dot.
        self.next_dot = None

    def tick(self):
        sys.stdout.write(".")
        self.col += 1
        if self.col >= self.width:
            sys.stdout.write("\n")
            self.col = 0
        sys.stdout.flush()

    def clear(self):
        if self.col:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.col = 0


def sleep_with_dots(seconds, beat):
    """Sleep, emitting a heartbeat dot every ``DOT_INTERVAL`` seconds of the wait."""
    end = time.monotonic() + seconds
    if beat.next_dot is None:
        beat.next_dot = time.monotonic() + DOT_INTERVAL
    while True:
        now = time.monotonic()
        if now >= end:
            return
        if now >= beat.next_dot:
            beat.tick()
            # Re-base on the actual tick rather than accumulating, so a slow
            # poll doesn't come back and fire a burst of catch-up dots.
            beat.next_dot = now + DOT_INTERVAL
            continue
        time.sleep(min(beat.next_dot, end) - now)


# ─── flag reading from a completion context ─────────────────────────────────

def flag_value(args, *names):
    """The value of ``--flag VALUE`` / ``--flag=VALUE`` in a token list.

    Completion contexts hand a completer every token typed so far except the
    consumed sub-command names (``ctx.args``), flags included.  A positional or
    value completer that needs to narrow by an already-typed flag — the hub for
    a content name, the category for a job name — reads it from here.
    """
    for i, tok in enumerate(args):
        for name in names:
            if tok == name and i + 1 < len(args):
                return args[i + 1]
            if tok.startswith(name + "="):
                return tok[len(name) + 1:]
    return None


def positionals(args, value_flags=()):
    """The positional tokens in ``ctx.args``, with flags and their values dropped.

    ``ctx.args`` carries flags interleaved with positionals, so a completer that
    keys off "the first positional" — the content name a version list belongs to,
    say — has to drop the flags *and* the token after each value-taking one.  A
    flag's value is indistinguishable from a positional otherwise: in
    ``--hub MyHub <TAB>``, ``MyHub`` is not the name being completed.
    """
    out = []
    skip = False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            skip = tok in value_flags        # `--flag=value` needs no skip
            continue
        out.append(tok)
    return out
