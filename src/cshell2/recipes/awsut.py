"""AWS utility commands ported from the legacy cshell.

Provides the ``awsut`` command tree:

* ``awsut console <page>``           — open a Management Console URL
* ``awsut recent-cost``              — show recent AWS cost
* ``awsut ec2 list|start|stop|reboot``
* ``awsut logs list|monitor|export``
* ``awsut cf list|wait|open``
* ``awsut hyperpod create|update|scale|add-ig|remove-ig|
  delete-nodes|reboot-nodes|replace-nodes|upgrade-ami|delete|
  list|describe|wait|log|ssm|ssh|run|search-capacity|
  kubeconfig|events``

Profile and region switching live in the ``aws`` recipe as ``Var`` entries
(``var aws_profile=...``, ``var aws_region=...``).  The SageMaker endpoint
and SageMaker service name are also exposed as ``Var`` entries — set them
at the prompt with ``var sagemaker_endpoint=...`` /
``var sagemaker_service_name=...`` (or ``var sagemaker_endpoint=`` to
unset).  These two are stored in module-level Python variables (not
``os.environ``), so they don't leak into subprocesses.

User-customisable defaults (read from ``~/.cshell2/config.py`` if set):

    from cshell2.recipes import awsut
    awsut.console_pages = {"home": "https://...", ...}
    awsut.console_url_modifier_func = lambda account, role, url: ...
    awsut.awscli = ["aws"]
"""

from __future__ import annotations

import concurrent.futures
import copy
import datetime
import fnmatch
import json
import os
import re
import threading
import time
import urllib.parse
import webbrowser
from typing import Callable

import boto3

from ..commands import registry as command_registry, arg
from ..completion import (
    ChoiceCompleter,
    Completer,
    Completion,
    CompletionContext,
    FileCompleter,
)
from ..shell import passthrough_input, passthrough_run
from ..variables import Var, registry as var_registry


# ─── User-customisable module-level config ──────────────────────────────────
#
# Override from ~/.cshell2/config.py:
#
#     from cshell2.recipes import awsut
#     awsut.console_pages = {...}
#     awsut.console_url_modifier_func = lambda account, role, url: ...

console_pages: dict[str, str] = {
    "home":     "https://console.aws.amazon.com/console/home",
    "s3":       "https://console.aws.amazon.com/s3/home",
    "iam":      "https://console.aws.amazon.com/iam/home",
    "cf":       "https://console.aws.amazon.com/cloudformation/home",
    "cost":     "https://console.aws.amazon.com/costmanagement/home#/cost-explorer?granularity=Daily&historicalRelativeRange=LAST_7_DAYS",
    "hyperpod": "https://console.aws.amazon.com/sagemaker/home#/cluster-management",
}

console_url_modifier_func: Callable[[str, str, str], str] | None = None

awscli: list[str] = ["aws"]

# Set via ``var sagemaker_endpoint=...`` / ``var sagemaker_service_name=...``
# at the prompt.  Module-level Python state (not env vars) so they don't
# leak into subprocesses.
sagemaker_endpoint: str = ""
sagemaker_service_name: str = "sagemaker"

_hyperpod_regions = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "ap-south-1", "ap-northeast-1", "ap-southeast-2",
]

_search_capacity_regions = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2", "ap-northeast-1",
]

_instance_type_choices = [
    "ml.trn1.32xlarge", "ml.p5.48xlarge", "ml.p5e.48xlarge",
    "ml.p5en.48xlarge", "ml.p4d.24xlarge", "ml.t3.xlarge",
    "ml.trn2.48xlarge", "ml.c4.large", "ml.c6i.large",
    "ml.t3.2xlarge", "ml.t3.large", "ml.c7g.medium",
]


# ─── boto3 / AWS helpers ────────────────────────────────────────────────────

def _get_boto3_client(service_name: str):
    region_name = os.environ.get("AWS_REGION")
    return boto3.client(service_name, region_name=region_name)


def _get_sagemaker_client(region_name: str | None = None):
    endpoint_url = sagemaker_endpoint or None
    if region_name is None:
        region_name = os.environ.get("AWS_REGION")
    return boto3.client(
        sagemaker_service_name,
        region_name=region_name,
        endpoint_url=endpoint_url,
    )


def _get_region() -> str | None:
    if "AWS_REGION" in os.environ:
        return os.environ["AWS_REGION"]
    try:
        return boto3.session.Session().region_name
    except Exception:
        return None


def _get_profile() -> str:
    return os.environ.get("AWS_PROFILE", "default")


def _get_all_profiles() -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    aws_config_path = os.path.expanduser("~/.aws/config")
    if not os.path.exists(aws_config_path):
        return profiles

    with open(aws_config_path) as fd:
        profile_name = ""
        for line in fd:
            stripped = line.strip()
            if re.match(r"\[default\]", stripped):
                profile_name = "default"
                profiles[profile_name] = {}
                continue
            m = re.match(r"\[profile\s(.+)\]", stripped)
            if m:
                profile_name = m.group(1)
                profiles[profile_name] = {}
                continue
            m = re.match(
                r"credential_process.*\-\-awscli\s+\b(\d{12})\b.*\-\-role\s+([A-Za-z0-9_-]+)",
                stripped,
            )
            if m and profile_name:
                profiles[profile_name]["account"] = m.group(1)
                profiles[profile_name]["role"] = m.group(2)
    return profiles


def _max_len(items, key) -> int:
    if not isinstance(key, (list, tuple)):
        key = [key]
    n = 0
    for it in items:
        v = it
        for k in key:
            v = v[k]
        n = max(n, len(v))
    return n


# ─── progress dots used by `cf wait` and `hyperpod wait` ────────────────────

class _ProgressDots:
    def __init__(self):
        self.status = None

    def tick(self, status):
        if self.status != status:
            if self.status is not None:
                print()
            self.status = status
            if self.status is not None:
                print(self.status, end=" ", flush=True)
            return
        if self.status is not None:
            print(".", end="", flush=True)


# ─── completers ─────────────────────────────────────────────────────────────

class _ConsolePageCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=name, description=url)
            for name, url in console_pages.items()
            if name.startswith(ctx.prefix)
        ]


class _CostServiceCompleter(Completer):
    """Completes service names from Cost Explorer (last 30 days)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        today = datetime.datetime.now().date()
        try:
            client = _get_boto3_client("ce")
            response = client.get_dimension_values(
                TimePeriod={
                    "Start": (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d"),
                    "End":   (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
                },
                Dimension="SERVICE",
            )
        except Exception:
            return []
        return [
            Completion(value=d["Value"])
            for d in response.get("DimensionValues", [])
            if d["Value"].startswith(ctx.prefix)
        ]


class _Ec2InstanceNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            ec2 = _get_boto3_client("ec2")
            response = ec2.describe_instances()
        except Exception:
            return []
        names: list[str] = []
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                for tag in instance.get("Tags", []) or []:
                    if tag["Key"] == "Name":
                        names.append(tag["Value"])
                        break
        return [Completion(value=n, description="ec2 instance")
                for n in names if n.startswith(ctx.prefix)]


def _list_log_groups(prefix: str = "") -> list[dict]:
    logs = _get_boto3_client("logs")
    groups: list[dict] = []
    next_token = None
    while True:
        params = {"limit": 50}
        if prefix:
            params["logGroupNamePrefix"] = prefix
        if next_token:
            params["nextToken"] = next_token
        response = logs.describe_log_groups(**params)
        groups += response["logGroups"]
        next_token = response.get("nextToken")
        if not next_token:
            break
    return groups


class _LogGroupCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            groups = _list_log_groups(ctx.prefix)
        except Exception:
            return []
        return [Completion(value=g["logGroupName"], description="log group")
                for g in groups if g["logGroupName"].startswith(ctx.prefix)]


class _LogStreamCompleter(Completer):
    """Completes log stream names. Reads the group_name from preceding args."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        group_name = ctx.args[0]
        try:
            logs = _get_boto3_client("logs")
            response = logs.describe_log_streams(logGroupName=group_name)
        except Exception:
            return []
        return [Completion(value=s["logStreamName"], description="log stream")
                for s in response.get("logStreams", [])
                if s["logStreamName"].startswith(ctx.prefix)]


def _list_cf_stacks(
    cf_client,
    *,
    include_deleted: bool = False,
    include_successfully_completed: bool = False,
    include_nested: bool = False,
) -> list[dict]:
    status_filter = {
        "CREATE_IN_PROGRESS", "CREATE_FAILED", "CREATE_COMPLETE",
        "ROLLBACK_IN_PROGRESS", "ROLLBACK_FAILED", "ROLLBACK_COMPLETE",
        "DELETE_IN_PROGRESS", "DELETE_FAILED", "DELETE_COMPLETE",
        "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_COMPLETE", "UPDATE_ROLLBACK_IN_PROGRESS",
        "UPDATE_ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE", "REVIEW_IN_PROGRESS",
        "IMPORT_IN_PROGRESS", "IMPORT_COMPLETE",
        "IMPORT_ROLLBACK_IN_PROGRESS", "IMPORT_ROLLBACK_FAILED",
        "IMPORT_ROLLBACK_COMPLETE",
    }
    if not include_deleted:
        status_filter.discard("DELETE_COMPLETE")
    if not include_successfully_completed:
        status_filter -= {
            "CREATE_COMPLETE", "DELETE_COMPLETE",
            "UPDATE_COMPLETE", "IMPORT_COMPLETE",
        }

    stacks: list[dict] = []
    next_token = None
    while True:
        params = {"StackStatusFilter": list(status_filter)}
        if next_token:
            params["NextToken"] = next_token
        response = cf_client.list_stacks(**params)
        stacks += response.get("StackSummaries", [])
        next_token = response.get("NextToken")
        if not next_token:
            break
    if not include_nested:
        stacks = [s for s in stacks if "ParentId" not in s]
    return stacks


class _CfStackNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            cf = _get_boto3_client("cloudformation")
            stacks = _list_cf_stacks(
                cf,
                include_deleted=False,
                include_successfully_completed=True,
                include_nested=True,
            )
        except Exception:
            return []
        return [Completion(value=s["StackName"], description=s["StackStatus"])
                for s in stacks if s["StackName"].startswith(ctx.prefix)]


# ─── HyperPod helpers ───────────────────────────────────────────────────────

def _drop(d, key):
    d.pop(key, None)


def _rename(d, old, new):
    if old in d:
        d[new] = d.pop(old)


def _sanitize_instance_group(ig: dict) -> dict:
    """Strip read-only fields from a describe_cluster InstanceGroup so it can be
    fed back into update_cluster.  Mutates and returns the same dict."""
    _rename(ig, "TargetCount", "InstanceCount")
    for k in ("CurrentCount", "TargetCount", "Status",
              "SoftwareUpdateStatus", "TargetStateCount",
              "ActiveOperations", "FailureMessages",
              "TrainingPlanStatus", "CurrentImageId",
              "ImageVersionStatus"):
        _drop(ig, k)
    _rename(ig, "DesiredImageId", "ImageId")
    if "KubernetesConfig" in ig:
        kc = ig["KubernetesConfig"]
        _rename(kc, "DesiredLabels", "Labels")
        _rename(kc, "DesiredTaints", "Taints")
        _drop(kc, "CurrentLabels")
        _drop(kc, "CurrentTaints")
    return ig


def _sanitize_restricted_instance_group(ig: dict) -> dict:
    """Same as `_sanitize_instance_group` but for RestrictedInstanceGroups."""
    _rename(ig, "TargetCount", "InstanceCount")
    for k in ("CurrentCount", "Status", "TrainingPlanStatus"):
        _drop(ig, k)
    if "EnvironmentConfig" in ig:
        _drop(ig["EnvironmentConfig"], "S3OutputPath")
    return ig


def _list_hyperpod_clusters_all(sagemaker_client) -> list[dict]:
    clusters: list[dict] = []
    next_token = None
    while True:
        params = {}
        if next_token:
            params["NextToken"] = next_token
        response = sagemaker_client.list_clusters(**params)
        clusters += response["ClusterSummaries"]
        next_token = response.get("NextToken")
        if not next_token:
            break
    return clusters


def _list_hyperpod_cluster_nodes_all(sagemaker_client, cluster_name: str) -> list[dict]:
    nodes: list[dict] = []
    next_token = None
    while True:
        params = {"ClusterName": cluster_name}
        if next_token:
            params["NextToken"] = next_token
        response = sagemaker_client.list_cluster_nodes(**params)
        nodes += response["ClusterNodeSummaries"]
        next_token = response.get("NextToken")
        if not next_token:
            break
    return nodes


def _list_hyperpod_cluster_events_all(sagemaker_client, cluster_name: str) -> list[dict]:
    events: list[dict] = []
    next_token = None
    while True:
        params = {"ClusterName": cluster_name}
        if next_token:
            params["NextToken"] = next_token
        response = sagemaker_client.list_cluster_events(**params)
        events += response["Events"]
        next_token = response.get("NextToken")
        if not next_token:
            break
    return events


def _list_hyperpod_log_streams_all(logs_client, log_group: str) -> list[dict]:
    streams: list[dict] = []
    next_token = None
    while True:
        params = {"logGroupName": log_group, "limit": 50}
        if next_token:
            params["nextToken"] = next_token
        response = logs_client.describe_log_streams(**params)
        streams += response["logStreams"]
        next_token = response.get("nextToken")
        if not next_token:
            break
    return streams


class _HyperpodHostnames:
    """Maps cluster InstanceId ↔ short private DNS hostname."""

    _instance: "_HyperpodHostnames | None" = None

    @classmethod
    def instance(cls) -> "_HyperpodHostnames":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.node_id_to_hostname: dict[str, str] = {}
        self.hostname_to_node_id: dict[str, str] = {}

    def resolve(self, sagemaker_client, cluster, nodes):
        cluster_name = cluster["ClusterName"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            def resolve_one(node):
                node_id = node["InstanceId"]
                cached = self.node_id_to_hostname.get(node_id)
                if cached:
                    return cached
                response = sagemaker_client.describe_cluster_node(
                    ClusterName=cluster_name, NodeId=node_id,
                )
                return response["NodeDetails"]["PrivateDnsHostname"].split(".")[0]

            for node, hostname in zip(nodes, pool.map(resolve_one, nodes)):
                self.node_id_to_hostname[node["InstanceId"]] = hostname
                self.hostname_to_node_id[hostname] = node["InstanceId"]

    def get_hostname(self, node_id: str) -> str | None:
        return self.node_id_to_hostname.get(node_id)

    def get_node_id(self, hostname: str) -> str | None:
        return self.hostname_to_node_id.get(hostname)


def _resolve_hyperpod_node_id(sm, cluster, cluster_name, node_id_input: str) -> str:
    """Strip instance-group prefix and convert hostname to node_id when needed."""
    if "/" in node_id_input:
        node_id_input = node_id_input.split("/")[-1]
    if node_id_input.startswith("ip-"):
        nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        hostnames = _HyperpodHostnames.instance()
        hostnames.resolve(sm, cluster, nodes)
        resolved = hostnames.get_node_id(node_id_input)
        if resolved:
            return resolved
    return node_id_input


def _print_hyperpod_log(logs_client, log_group, stream):
    start_time = int((time.time() - 24 * 60 * 60) * 1000)
    next_token = None
    try:
        while True:
            params = {
                "logGroupName":  log_group,
                "logStreamName": stream,
                "startFromHead": True,
                "limit":         1000,
            }
            if next_token:
                params["nextToken"] = next_token
            else:
                params["startTime"] = start_time
            try:
                response = logs_client.get_log_events(**params)
            except logs_client.exceptions.ResourceNotFoundException:
                print(f"Log group or stream not found [ {log_group}, {stream} ]")
                return

            for event in response["events"]:
                if start_time > event["timestamp"]:
                    continue
                print(event["message"].replace("\0", "\\0"))

            if response["nextForwardToken"] != next_token:
                next_token = response["nextForwardToken"]
            else:
                break
    except BrokenPipeError:
        # Downstream consumer closed early (e.g. `... | head`).  Normal.
        pass


# ─── shared SSM-pexpect helper ──────────────────────────────────────────────
#
# Both `awsut hyperpod run` and `awsut hyperpod ssh` need to drive
# a remote bash session over `aws ssm start-session`. The shape is identical:
# spawn the SSM client under a real PTY (so the remote shell sees a tty and
# prints prompts), wait for an initial prompt, send a single shell line, wait
# for the command to finish, then close cleanly. `_ssm_run` is the single
# place that touches pexpect; callers just describe the work as a shell line
# and decide whether they want the captured output.

# Initial-prompt patterns covering AL2 (`sh-4.2#`), AL2023 (`sh-5.2#`), and
# generic `# ` / `$ `. The TIMEOUT sentinel at the end is a fallback the
# caller can detect to nudge a prompt out of a terse SSM banner.
_SSM_PROMPT_PATTERNS = [
    r"sh-\d+\.\d+[#\$]\s*",
    r"[#\$]\s+",
]


def _ssm_run(
    ssm_target: str,
    shell_line: str,
    *,
    capture: bool = True,
    timeout: int = 30,
) -> tuple[str | None, str | None]:
    """Run a single shell line in an SSM session and return its output.

    Returns ``(output, error)`` where exactly one is non-None on success
    paths. ``output`` is the captured stdout/stderr of the shell line with
    line endings normalized to ``\\n``; ``error`` is a short one-line
    description of any pexpect-level failure (TIMEOUT, EOF, etc.). When
    ``capture=False`` the function still waits for completion but returns
    ``("", None)`` — useful for "fire and check" callers like install-key
    that just need to know the command finished.

    The end-of-output sentinel is built from two shell variables joined at
    runtime (``$S$T``) so that pexpect's first match for the literal
    sentinel string can only fire on the *resolved* echo line, not on the
    echo of the input line itself.
    """
    import pexpect

    sentinel = "__cshell2_ssm_done_aef36c__"
    head = sentinel[: len(sentinel) // 2]
    tail = sentinel[len(sentinel) // 2 :]
    initial_patterns = [*_SSM_PROMPT_PATTERNS, pexpect.TIMEOUT]

    p = None
    try:
        p = pexpect.spawn(
            awscli[0],
            args=[*awscli[1:], "ssm", "start-session", "--target", ssm_target],
            timeout=timeout,
            encoding="utf-8",
        )
        idx = p.expect(initial_patterns, timeout=timeout)
        if idx == len(initial_patterns) - 1:
            # SSM banner ended without a prompt — nudge with a bare newline.
            p.sendline("")
            p.expect(_SSM_PROMPT_PATTERNS, timeout=10)

        p.sendline(f'S="{head}"; T="{tail}"; {shell_line}; echo "$S$T"')
        p.expect(sentinel, timeout=timeout)

        output = ""
        if capture:
            # Collapse PTY-doubled CRs ("\r\r\n") down to a single \n in one
            # pass — a naive \r\n→\n then \r→\n turns "\r\r\n" into "\n\n".
            raw = re.sub(r"\r+\n?", "\n", p.before or "")
            # First newline-delimited chunk is the shell's echo of our
            # sendline. Drop it; everything after, up to the sentinel, is
            # the real output. Strip the trailing `echo "$S$T"` echo line
            # bash emits just before the marker.
            _, _, after_echo = raw.partition("\n")
            output = re.sub(r'echo "\$S\$T"\s*\n?$', "", after_echo)

        # Graceful close — let SSM tear down rather than killing it.
        p.sendline("exit")
        try:
            p.expect(pexpect.EOF, timeout=5)
        except pexpect.TIMEOUT:
            pass
        return output, None
    except pexpect.TIMEOUT:
        tail_bytes = (p.before[-200:] if p is not None and p.before else "")
        return None, f"TIMEOUT (tail: {tail_bytes!r})" if tail_bytes else "TIMEOUT"
    except pexpect.EOF:
        tail_bytes = (p.before[-200:] if p is not None and p.before else "")
        return (None,
                f"EOF: SSM session ended before command completed "
                f"(tail: {tail_bytes!r})" if tail_bytes else
                "EOF: SSM session ended before command completed")
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else ""
        return None, f"{type(e).__name__}{': ' + msg if msg else ''}"
    finally:
        if p is not None and p.isalive():
            try:
                p.terminate(force=True)
            except Exception:
                pass


# ─── HyperPod completers ────────────────────────────────────────────────────

class _HyperpodClusterNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            sm = _get_sagemaker_client()
            clusters = _list_hyperpod_clusters_all(sm)
        except Exception:
            return []
        return [Completion(value=c["ClusterName"], description=c.get("ClusterStatus", ""))
                for c in clusters
                if c["ClusterName"].startswith(ctx.prefix)]


class _HyperpodInstanceGroupNameCompleter(Completer):
    """Completes instance group names. Reads cluster_name from preceding args."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        cluster_name = ctx.args[0]
        try:
            sm = _get_sagemaker_client()
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except Exception:
            return []
        result: list[Completion] = []
        for ig in (cluster.get("InstanceGroups", [])
                   + cluster.get("RestrictedInstanceGroups", [])):
            name = ig["InstanceGroupName"]
            if not name.startswith(ctx.prefix):
                continue
            instance_type = ig.get("InstanceType", "")
            current = ig.get("CurrentCount")
            target = ig.get("TargetCount")
            if current is not None and target is not None:
                count = f"{current}=>{target}" if current != target else f"{current}"
            elif target is not None:
                count = str(target)
            elif current is not None:
                count = str(current)
            else:
                count = ""
            desc = " ".join(p for p in (instance_type, f"({count})" if count else "") if p)
            result.append(Completion(value=name, description=desc))
        return result


class _HyperpodSubnetIdCompleter(Completer):
    """Completes subnet IDs in the same VPC as the cluster.

    Reads ``cluster_name`` from ``ctx.args[0]``.  Locates the VPC by looking at
    a known subnet on the cluster (any IG's ``OverrideVpcConfig.Subnets`` or
    the cluster-level ``VpcConfig.Subnets``), then lists every subnet in that
    VPC.  Each completion is annotated with the subnet's Name tag and AZ.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        cluster_name = ctx.args[0]
        try:
            sm = _get_sagemaker_client()
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except Exception:
            return []

        seed_subnet = None
        for ig in cluster.get("InstanceGroups", []) + cluster.get("RestrictedInstanceGroups", []):
            ovc = ig.get("OverrideVpcConfig") or {}
            subnets = ovc.get("Subnets") or []
            if subnets:
                seed_subnet = subnets[0]
                break
        if seed_subnet is None:
            cluster_vpc = cluster.get("VpcConfig") or {}
            subnets = cluster_vpc.get("Subnets") or []
            if subnets:
                seed_subnet = subnets[0]
        if seed_subnet is None:
            return []

        try:
            ec2 = _get_boto3_client("ec2")
            seed_resp = ec2.describe_subnets(SubnetIds=[seed_subnet])
            vpc_id = seed_resp["Subnets"][0]["VpcId"]
            vpc_resp = ec2.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
            )
        except Exception:
            return []

        result: list[Completion] = []
        for subnet in vpc_resp.get("Subnets", []):
            sid = subnet["SubnetId"]
            if not sid.startswith(ctx.prefix):
                continue
            name = ""
            for tag in subnet.get("Tags", []) or []:
                if tag.get("Key") == "Name":
                    name = tag.get("Value", "")
                    break
            az_id = subnet.get("AvailabilityZoneId", "")
            cidr = subnet.get("CidrBlock", "")
            desc = " ".join(p for p in (az_id, cidr, name) if p)
            result.append(Completion(value=sid, description=desc))
        return result


class _HyperpodNodeIdCompleter(Completer):
    """Completes node IDs (and IG/node_id, hostname). Reads cluster_name from preceding args."""

    def __init__(self, with_cwlog: bool = False):
        self.with_cwlog = with_cwlog

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        cluster_name = ctx.args[0]
        try:
            sm = _get_sagemaker_client()
            logs = _get_boto3_client("logs")
            cluster = sm.describe_cluster(ClusterName=cluster_name)
            nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        except Exception:
            return []

        hostnames = _HyperpodHostnames.instance()
        try:
            hostnames.resolve(sm, cluster, nodes)
        except Exception:
            pass

        choices: list[str] = []
        for node in nodes:
            node_id = node["InstanceId"]
            ig_name = node["InstanceGroupName"]
            choices.append(node_id)
            hn = hostnames.get_hostname(node_id)
            if hn:
                choices.append(hn)
            choices.append(f"{ig_name}/{node_id}")

        if self.with_cwlog:
            cluster_id = cluster["ClusterArn"].split("/")[-1]
            log_group = f"/aws/sagemaker/Clusters/{cluster_name}/{cluster_id}"
            try:
                streams = _list_hyperpod_log_streams_all(logs, log_group)
            except Exception:
                streams = []
            for stream in streams:
                stream_name = stream["logStreamName"]
                parts = stream_name.split("/")
                if len(parts) >= 2:
                    ig_name = parts[-2]
                    node_id = parts[-1]
                    choices.append(node_id)
                    choices.append(f"{ig_name}/{node_id}")

        seen: set[str] = set()
        unique: list[str] = []
        for c in choices:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return [Completion(value=c, description="node")
                for c in unique if c.startswith(ctx.prefix)]


# ─── module-level Python-backed Vars ────────────────────────────────────────

class _SagemakerEndpointVar(Var):
    name = "sagemaker_endpoint"
    description = "SageMaker endpoint URL (blank = AWS default)"

    def get(self) -> str | None:
        return sagemaker_endpoint or None

    def set(self, value: str) -> None:
        global sagemaker_endpoint
        sagemaker_endpoint = value

    def unset(self) -> None:
        global sagemaker_endpoint
        sagemaker_endpoint = ""


class _SagemakerServiceNameVar(Var):
    name = "sagemaker_service_name"
    description = "boto3 service name for SageMaker client (default: sagemaker)"

    def get(self) -> str | None:
        return sagemaker_service_name

    def set(self, value: str) -> None:
        global sagemaker_service_name
        sagemaker_service_name = value or "sagemaker"

    def unset(self) -> None:
        global sagemaker_service_name
        sagemaker_service_name = "sagemaker"


# ─── tree definition ────────────────────────────────────────────────────────

def register() -> None:
    awsut = command_registry.command("awsut", help="AWS utility commands")

    _register_console(awsut)
    _register_recent_cost(awsut)
    _register_ec2(awsut)
    _register_logs(awsut)
    _register_cf(awsut)
    _register_hyperpod(awsut)

    var_registry.register(_SagemakerEndpointVar())
    var_registry.register(_SagemakerServiceNameVar())


def _register_console(awsut) -> None:
    @awsut.command(
        "console",
        help="Open Management Console",
        params=[arg("page_name", completer=_ConsolePageCompleter())],
    )
    def _console(page_name):
        if page_name not in console_pages:
            print(f"Unknown console page: {page_name}")
            return
        url = console_pages[page_name]

        region = _get_region() or "us-east-1"
        parsed = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed.query)
        query_params["region"] = [region]
        new_query = urllib.parse.urlencode(query_params, doseq=True)
        url = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path,
             parsed.params, new_query, parsed.fragment)
        )

        if console_url_modifier_func is not None:
            profile_name = _get_profile()
            profile = _get_all_profiles().get(profile_name, {})
            account = profile.get("account", "")
            role = profile.get("role", "")
            url = console_url_modifier_func(account, role, url)

        print(f"Opening {url}")
        webbrowser.open(url)


def _register_recent_cost(awsut) -> None:
    @awsut.command(
        "recent-cost",
        help="Show recent cost",
        params=[
            arg("--days", type=int, default=14, metavar="N",
                help="Number of days to show"),
            arg("--filter", nargs="+", metavar="SERVICE",
                completer=_CostServiceCompleter(),
                help="Filter by service name(s) and show usage type breakdown"),
            arg("--min", type=float, default=0.1, dest="min_amount",
                metavar="AMOUNT",
                help="Hide rows/columns with total below this amount"),
        ],
    )
    def _recent_cost(days, filter, min_amount):
        client = _get_boto3_client("ce")

        today = datetime.datetime.now().date()
        period_end = today + datetime.timedelta(days=1)
        period_start = period_end - datetime.timedelta(days=days)

        ce_params = {
            "TimePeriod": {
                "Start": period_start.strftime("%Y-%m-%d"),
                "End":   period_end.strftime("%Y-%m-%d"),
            },
            "Granularity": "MONTHLY",
            "Metrics": ["AmortizedCost"],
        }

        if filter:
            ce_params["Filter"] = {"Dimensions": {"Key": "SERVICE", "Values": filter}}
            ce_params["GroupBy"] = [
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                {"Type": "DIMENSION", "Key": "REGION"},
            ]
            row_label = "Usage Type"
            title = f"Usage Type x Region: {', '.join(filter)} (last {days} days)"
        else:
            ce_params["GroupBy"] = [
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "REGION"},
            ]
            row_label = "Service"
            title = f"Service x Region (last {days} days)"

        response = client.get_cost_and_usage(**ce_params)

        data: dict[str, dict[str, float]] = {}
        region_totals: dict[str, float] = {}
        row_totals: dict[str, float] = {}

        for item in response["ResultsByTime"]:
            for group in item["Groups"]:
                row_key = group["Keys"][0]
                region = group["Keys"][1]
                amount = float(group["Metrics"]["AmortizedCost"]["Amount"])
                if amount < 0.01:
                    continue
                data.setdefault(row_key, {})[region] = (
                    data.get(row_key, {}).get(region, 0.0) + amount
                )
                region_totals[region] = region_totals.get(region, 0.0) + amount
                row_totals[row_key]   = row_totals.get(row_key, 0.0) + amount

        regions = sorted(
            [r for r, t in region_totals.items() if t >= min_amount],
            key=lambda r: region_totals[r], reverse=True,
        )
        rows = sorted(
            [r for r, t in row_totals.items() if t >= min_amount],
            key=lambda s: row_totals[s], reverse=True,
        )

        if not regions or not rows:
            print("\nNo cost data for table.")
            return

        all_values = sorted(
            [v for row_data in data.values() for v in row_data.values()],
            reverse=True,
        )
        max_val = all_values[0] if all_values else 0
        top_10_pct_idx = max(1, len(all_values) // 10)
        yellow_threshold = all_values[top_10_pct_idx - 1] if all_values else 0

        RED, YELLOW, RESET = "\033[91m", "\033[93m", "\033[0m"

        def colorize(val, text):
            if val >= max_val:
                return f"{RED}{text}{RESET}"
            if val >= yellow_threshold:
                return f"{YELLOW}{text}{RESET}"
            return text

        row_col_width = max(len(r) for r in rows)
        col_widths = {region: max(len(region), 10) for region in regions}
        total_col_width = max(len("TOTAL"), 10)

        header = f"  {row_label:<{row_col_width}}"
        for region in regions:
            header += f"  {region:>{col_widths[region]}}"
        header += f"  {'TOTAL':>{total_col_width}}"
        print(f"\n=== {title} ===")
        print(header)
        print("  " + "-" * (len(header) - 2))

        for row_key in rows:
            line = f"  {row_key:<{row_col_width}}"
            for region in regions:
                w = col_widths[region]
                val = data.get(row_key, {}).get(region, 0.0)
                if val >= 0.01:
                    cell = f"{val:{w}.2f}"
                    line += f"  {colorize(val, cell)}"
                else:
                    line += f"  {'':>{w}}"
            total_cell = f"{row_totals[row_key]:{total_col_width}.2f}"
            line += f"  {total_cell}"
            print(line)

        footer = f"  {'TOTAL':<{row_col_width}}"
        for region in regions:
            footer += f"  {region_totals[region]:{col_widths[region]}.2f}"
        footer += f"  {sum(row_totals.values()):{total_col_width}.2f}"
        print("  " + "-" * (len(header) - 2))
        print(footer)


def _register_ec2(awsut) -> None:
    ec2 = awsut.command("ec2", help="EC2 commands")

    @ec2.command("list", help="List EC2 instances with status")
    def _ec2_list():
        ec2_client = _get_boto3_client("ec2")
        response = ec2_client.describe_instances()

        print("Existing instances:")
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                instance_id = instance["InstanceId"]
                name = ""
                for tag in instance.get("Tags", []) or []:
                    if tag["Key"] == "Name":
                        name = tag["Value"]
                        break
                state = instance["State"]["Name"]
                public_dns = ""
                for ni in instance.get("NetworkInterfaces", []):
                    if "Association" in ni:
                        public_dns = ni["Association"]["PublicDnsName"]
                        break
                print(f"  {name:>20} : {instance_id:<19} : {state:<8} : {public_dns}")

    def _ec2_match(instance, name) -> bool:
        for tag in instance.get("Tags", []) or []:
            if tag["Key"] == "Name" and tag["Value"] == name:
                return True
        return False

    def _ec2_action(instance_name, action_name, api_method):
        ec2_client = _get_boto3_client("ec2")
        response = ec2_client.describe_instances()
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                if _ec2_match(instance, instance_name):
                    api_method(InstanceIds=[instance["InstanceId"]])
                    print(f"{action_name}: {instance['InstanceId']}")
                    return
        print(f"Error : EC2 instance [{instance_name}] not found.")

    @ec2.command(
        "start", help="Start instance by name",
        params=[arg("instance_name", completer=_Ec2InstanceNameCompleter())],
    )
    def _ec2_start(instance_name):
        ec2_client = _get_boto3_client("ec2")
        _ec2_action(instance_name, "Started", ec2_client.start_instances)

    @ec2.command(
        "stop", help="Stop instance by name",
        params=[arg("instance_name", completer=_Ec2InstanceNameCompleter())],
    )
    def _ec2_stop(instance_name):
        ec2_client = _get_boto3_client("ec2")
        _ec2_action(instance_name, "Stopped", ec2_client.stop_instances)

    @ec2.command(
        "reboot", help="Reboot instance by name",
        params=[arg("instance_name", completer=_Ec2InstanceNameCompleter())],
    )
    def _ec2_reboot(instance_name):
        ec2_client = _get_boto3_client("ec2")
        _ec2_action(instance_name, "Rebooted", ec2_client.reboot_instances)


def _register_logs(awsut) -> None:
    logs = awsut.command("logs", help="CloudWatch Logs commands")

    @logs.command(
        "list", help="List log groups",
        params=[arg("group_name", nargs="?",
                    completer=_LogGroupCompleter(),
                    help="Log group name pattern with widecards")],
    )
    def _logs_list(group_name=None):
        logs_client = _get_boto3_client("logs")
        pattern = group_name if group_name is not None else "*"

        prefix = pattern
        for ch in ("*", "?"):
            pos = prefix.find(ch)
            if pos >= 0:
                prefix = prefix[:pos]

        last_found = None
        num_found = 0

        print("Log groups:")
        for lg in _list_log_groups(prefix):
            if fnmatch.fnmatch(lg["logGroupName"], pattern):
                print(f"  {lg['logGroupName']}")
                last_found = lg
                num_found += 1

        if num_found == 1:
            print()
            print("Streams:")
            response = logs_client.describe_log_streams(
                logGroupName=last_found["logGroupName"])
            for stream in response.get("logStreams", []):
                print(f"  {stream['logStreamName']}")

    @logs.command(
        "monitor", help="Monitor a log stream",
        params=[
            arg("group_name",  completer=_LogGroupCompleter(),
                help="Log group name"),
            arg("stream_name", completer=_LogStreamCompleter(),
                help="Log stream name to monitor"),
            arg("--freq",     type=int, default=5, metavar="SECONDS",
                help="Polling frequency in seconds"),
            arg("--lookback", type=int, default=60, metavar="MINUTES",
                help="Lookback window in minutes"),
        ],
    )
    def _logs_monitor(group_name, stream_name, freq, lookback):
        import sys
        logs_client = _get_boto3_client("logs")
        start_time = int((time.time() - lookback * 60) * 1000)

        next_token = None
        try:
            while True:
                params = {
                    "logGroupName":  group_name,
                    "logStreamName": stream_name,
                    "startFromHead": True,
                    "limit":         1000,
                }
                if next_token:
                    params["nextToken"] = next_token
                else:
                    params["startTime"] = start_time

                try:
                    response = logs_client.get_log_events(**params)
                except logs_client.exceptions.ResourceNotFoundException:
                    print(f"Log group or stream not found [ {group_name}, {stream_name} ]")
                    return

                for event in response["events"]:
                    if start_time > event["timestamp"]:
                        continue
                    msg = event["message"].replace("\0", "\\0")
                    print(msg)
                # Flush so a piped consumer (e.g. `... | grep ERROR`) sees
                # output as it arrives instead of one block-buffered chunk.
                sys.stdout.flush()

                if response["nextForwardToken"] != next_token:
                    next_token = response["nextForwardToken"]
                else:
                    # Short sleeps with a flush each tick — in a pipeline,
                    # Ctrl+C closes our stdout and the next flush() raises
                    # promptly instead of blocking until the full freq sec.
                    for _ in range(freq * 10):
                        time.sleep(0.1)
                        sys.stdout.flush()
        except (KeyboardInterrupt, BrokenPipeError):
            pass

    @logs.command(
        "export", help="Export a log group in a Zip file",
        params=[
            arg("group_name", completer=_LogGroupCompleter(),
                help="Log group name to export"),
            arg("s3_path", help="S3 path as a working place"),
            arg("--start-datetime", required=True, metavar="YYYYMMDD_HHMMSS",
                help="Start date-time in UTC, in YYYYMMDD_HHMMSS format"),
            arg("--end-datetime",   required=True, metavar="YYYYMMDD_HHMMSS",
                help="End date-time in UTC, in YYYYMMDD_HHMMSS format"),
        ],
    )
    def _logs_export(group_name, s3_path, start_datetime, end_datetime):
        exporter = _LogsExporter(
            logs_client=_get_boto3_client("logs"),
            log_group=group_name,
            s3_path=s3_path,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )
        exporter.run()


def _register_cf(awsut) -> None:
    cf = awsut.command("cf", help="CloudFormation commands")

    @cf.command(
        "list", help="List CloudFormation stacks",
        params=[
            arg("--include-deleted", action="store_true",
                help="Include deleted stacks"),
            arg("--include-nested", action="store_true",
                help="Include nested stacks"),
        ],
    )
    def _cf_list(include_deleted, include_nested):
        cf_client = _get_boto3_client("cloudformation")
        stacks = _list_cf_stacks(
            cf_client,
            include_deleted=include_deleted,
            include_successfully_completed=True,
            include_nested=include_nested,
        )
        if not stacks:
            print("No stacks.")
            return
        name_w   = _max_len(stacks, "StackName")
        status_w = _max_len(stacks, "StackStatus")
        for stack in stacks:
            nested = "ParentId" in stack
            print(
                f"{stack['StackName']:<{name_w}} : "
                f"{stack['StackStatus']:<{status_w}} : "
                f"{'(nested)' if nested else ''}"
            )

    @cf.command("wait", help="Wait until all CloudFormation operations finish")
    def _cf_wait():
        cf_client = _get_boto3_client("cloudformation")
        progress = _ProgressDots()
        while True:
            status_list = []
            stacks = _list_cf_stacks(cf_client, include_nested=True)
            for stack in stacks:
                if stack["StackStatus"].endswith("_IN_PROGRESS"):
                    status_list.append(f"{stack['StackName']}:{stack['StackStatus']}")
            progress.tick(", ".join(status_list))
            if not status_list:
                progress.tick(None)
                break
            time.sleep(5)

    @cf.command(
        "open", help="Open the CloudFormation management console",
        params=[arg("stack_name", completer=_CfStackNameCompleter())],
    )
    def _cf_open(stack_name):
        region = _get_region() or "us-east-1"
        cf_client = _get_boto3_client("cloudformation")
        stacks = _list_cf_stacks(
            cf_client,
            include_deleted=False,
            include_successfully_completed=True,
            include_nested=True,
        )
        stack_arn = next(
            (s["StackId"] for s in stacks if s["StackName"] == stack_name),
            None,
        )
        if not stack_arn:
            print(f"Stack [{stack_name}] not found.")
            return

        encoded = urllib.parse.quote(stack_arn)
        url = (f"https://{region}.console.aws.amazon.com/cloudformation/home"
               f"?region={region}#/stacks?stackId={encoded}")

        if console_url_modifier_func is not None:
            profile_name = _get_profile()
            profile = _get_all_profiles().get(profile_name, {})
            url = console_url_modifier_func(
                profile.get("account", ""), profile.get("role", ""), url,
            )

        print(f"Opening {url}")
        webbrowser.open(url)


def _register_hyperpod(awsut) -> None:
    hyperpod = awsut.command("hyperpod", help="SageMaker HyperPod cluster operations")

    @hyperpod.command(
        "create", help="Create a cluster with JSON file",
        params=[
            arg("cluster_name", help="Name of cluster"),
            arg("--eks-cluster-name", metavar="NAME",
                help="Name of EKS cluster"),
            arg("--instances", required=True, metavar="FILE",
                completer=FileCompleter(),
                help="JSON config file for instance groups"),
            arg("--restricted-instances", metavar="FILE",
                completer=FileCompleter(),
                help="JSON config file for restricted instance groups"),
            arg("--vpc", metavar="FILE", completer=FileCompleter(),
                help="JSON config file for VPC"),
        ],
    )
    def _create(cluster_name, eks_cluster_name, instances,
                restricted_instances, vpc):
        params = {
            "ClusterName":  cluster_name,
            "NodeRecovery": "Automatic",
        }
        if eks_cluster_name:
            eks = _get_boto3_client("eks")
            desc = eks.describe_cluster(name=eks_cluster_name)
            params["Orchestrator"] = {"Eks": {"ClusterArn": desc["cluster"]["arn"]}}
            params["NodeProvisioningMode"] = "Continuous"

        with open(os.path.expanduser(instances)) as fd:
            params["InstanceGroups"] = json.loads(fd.read())

        if restricted_instances:
            with open(os.path.expanduser(restricted_instances)) as fd:
                params["RestrictedInstanceGroups"] = json.loads(fd.read())

        if vpc:
            with open(os.path.expanduser(vpc)) as fd:
                params["VpcConfig"] = json.loads(fd.read())

        sm = _get_sagemaker_client()
        response = sm.create_cluster(**params)
        print(f"Creation started : {response['ClusterArn']}")

    @hyperpod.command(
        "update", help="Update a cluster with JSON file",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter(),
                help="Name of cluster"),
            arg("--instances", required=True, metavar="FILE",
                completer=FileCompleter(),
                help="JSON config file for instance groups"),
            arg("--restricted-instances", metavar="FILE",
                completer=FileCompleter(),
                help="JSON config file for restricted instance groups"),
        ],
    )
    def _update(cluster_name, instances, restricted_instances):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        params = {"ClusterName": cluster_name}
        if "NodeRecovery" in cluster:
            params["NodeRecovery"] = cluster["NodeRecovery"]

        with open(os.path.expanduser(instances)) as fd:
            params["InstanceGroups"] = json.loads(fd.read())

        if restricted_instances:
            with open(os.path.expanduser(restricted_instances)) as fd:
                params["RestrictedInstanceGroups"] = json.loads(fd.read())

        response = sm.update_cluster(**params)
        print(f"Updating cluster started : {response['ClusterArn']}")

    @hyperpod.command(
        "scale", help="Scale up or down an instance group",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("instance_group_name", completer=_HyperpodInstanceGroupNameCompleter()),
            arg("target_instance_count", type=int),
        ],
    )
    def _scale(cluster_name, instance_group_name, target_instance_count):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        params = {"ClusterName": cluster_name}
        if "NodeRecovery" in cluster:
            params["NodeRecovery"] = cluster["NodeRecovery"]

        if cluster.get("InstanceGroups"):
            params["InstanceGroups"] = []
        for ig in cluster.get("InstanceGroups", []):
            _sanitize_instance_group(ig)
            if ig["InstanceGroupName"] == instance_group_name:
                ig["InstanceCount"] = target_instance_count
            params["InstanceGroups"].append(ig)

        if cluster.get("RestrictedInstanceGroups"):
            params["RestrictedInstanceGroups"] = []
        for ig in cluster.get("RestrictedInstanceGroups", []):
            _sanitize_restricted_instance_group(ig)
            if ig["InstanceGroupName"] == instance_group_name:
                ig["InstanceCount"] = target_instance_count
            params["RestrictedInstanceGroups"].append(ig)

        response = sm.update_cluster(**params)
        print(f"Updating cluster started : {response['ClusterArn']}")

    @hyperpod.command(
        "add-ig",
        help="Create a new instance group based on an existing template",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("instance_group_name", help="Name of the new instance group"),
            arg("--template", required=True, metavar="NAME",
                completer=_HyperpodInstanceGroupNameCompleter(),
                help="Existing instance group to copy as the template"),
            arg("--instance-type", metavar="TYPE",
                completer=ChoiceCompleter(_instance_type_choices),
                help="Override instance type (default: copy from template)"),
            arg("--instance-count", type=int, default=0, metavar="N",
                help="Initial instance count (default: 0; scale up later "
                     "with `awsut hyperpod scale`)"),
            arg("--subnet-id", metavar="SUBNET",
                completer=_HyperpodSubnetIdCompleter(),
                help="Override subnet ID. Security groups are inherited from "
                     "the template's OverrideVpcConfig if present, otherwise "
                     "from the cluster's VpcConfig."),
        ],
    )
    def _create_instance_group(cluster_name, instance_group_name, template,
                               instance_type, instance_count, subnet_id):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        # Locate the template across both regular and restricted IGs.
        template_ig = None
        is_restricted = False
        for ig in cluster.get("InstanceGroups", []):
            if ig["InstanceGroupName"] == template:
                template_ig = ig
                break
        if template_ig is None:
            for ig in cluster.get("RestrictedInstanceGroups", []):
                if ig["InstanceGroupName"] == template:
                    template_ig = ig
                    is_restricted = True
                    break
        if template_ig is None:
            print(f"Template instance group [{template}] not found in cluster "
                  f"[{cluster_name}].")
            return

        existing_names = {ig["InstanceGroupName"]
                          for ig in cluster.get("InstanceGroups", [])}
        existing_names |= {ig["InstanceGroupName"]
                           for ig in cluster.get("RestrictedInstanceGroups", [])}
        if instance_group_name in existing_names:
            print(f"Instance group [{instance_group_name}] already exists in "
                  f"cluster [{cluster_name}].")
            return

        # Sanitize first (strips read-only fields and renames TargetCount →
        # InstanceCount), THEN apply overrides — otherwise the rename would
        # clobber a user-supplied --instance-count with the template's
        # TargetCount.
        new_ig = copy.deepcopy(template_ig)
        if is_restricted:
            _sanitize_restricted_instance_group(new_ig)
        else:
            _sanitize_instance_group(new_ig)

        new_ig["InstanceGroupName"] = instance_group_name
        new_ig["InstanceCount"] = instance_count
        if instance_type is not None:
            new_ig["InstanceType"] = instance_type
        if subnet_id is not None:
            existing_override = template_ig.get("OverrideVpcConfig") or {}
            sgs = existing_override.get("SecurityGroupIds")
            if not sgs:
                cluster_vpc = cluster.get("VpcConfig") or {}
                sgs = cluster_vpc.get("SecurityGroupIds")
            if not sgs:
                print("Could not infer SecurityGroupIds for the new instance "
                      "group: neither the template's OverrideVpcConfig nor the "
                      "cluster's VpcConfig has them.")
                return
            new_ig["OverrideVpcConfig"] = {
                "Subnets": [subnet_id],
                "SecurityGroupIds": list(sgs),
            }

        # Build update_cluster params: keep all existing IGs untouched
        # (after sanitizing) and append the new one.
        params = {"ClusterName": cluster_name}
        if "NodeRecovery" in cluster:
            params["NodeRecovery"] = cluster["NodeRecovery"]

        regular = [_sanitize_instance_group(ig)
                   for ig in cluster.get("InstanceGroups", [])]
        restricted = [_sanitize_restricted_instance_group(ig)
                      for ig in cluster.get("RestrictedInstanceGroups", [])]

        if is_restricted:
            restricted.append(new_ig)
        else:
            regular.append(new_ig)

        if regular:
            params["InstanceGroups"] = regular
        if restricted:
            params["RestrictedInstanceGroups"] = restricted

        response = sm.update_cluster(**params)
        print(f"Creating instance group [{instance_group_name}] started : "
              f"{response['ClusterArn']}")

    @hyperpod.command(
        "remove-ig", help="Delete an instance group from a cluster",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("instance_group_name", completer=_HyperpodInstanceGroupNameCompleter()),
            arg("-y", "--yes", action="store_true", help="Skip confirmation"),
        ],
    )
    def _delete_instance_group(cluster_name, instance_group_name, yes):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        regular_names = [ig["InstanceGroupName"]
                         for ig in cluster.get("InstanceGroups", [])]
        restricted_names = [ig["InstanceGroupName"]
                            for ig in cluster.get("RestrictedInstanceGroups", [])]
        if (instance_group_name not in regular_names
                and instance_group_name not in restricted_names):
            print(f"Instance group [{instance_group_name}] not found in cluster "
                  f"[{cluster_name}].")
            return

        if not yes:
            answer = passthrough_input(
                f"Are you sure deleting instance group [{instance_group_name}] "
                f"from cluster [{cluster_name}]? [y/N] : "
            )
            if answer.lower() not in ("y", "yes"):
                return

        params = {"ClusterName": cluster_name}
        if "NodeRecovery" in cluster:
            params["NodeRecovery"] = cluster["NodeRecovery"]

        regular = [_sanitize_instance_group(ig)
                   for ig in cluster.get("InstanceGroups", [])
                   if ig["InstanceGroupName"] != instance_group_name]
        restricted = [_sanitize_restricted_instance_group(ig)
                      for ig in cluster.get("RestrictedInstanceGroups", [])
                      if ig["InstanceGroupName"] != instance_group_name]

        if regular:
            params["InstanceGroups"] = regular
        if restricted:
            params["RestrictedInstanceGroups"] = restricted

        response = sm.update_cluster(**params)
        print(f"Deleting instance group [{instance_group_name}] started : "
              f"{response['ClusterArn']}")

    def _batch_node_operation(operation_name, api, cluster_name, node_ids):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return
        resolved_ids = [
            _resolve_hyperpod_node_id(sm, cluster, cluster_name, n) for n in node_ids
        ]
        response = api(ClusterName=cluster_name, NodeIds=resolved_ids)
        print(f"{operation_name} : {response}")

    @hyperpod.command(
        "delete-nodes", help="Delete specific nodes",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_HyperpodNodeIdCompleter(with_cwlog=False)),
        ],
    )
    def _delete_nodes(cluster_name, node_ids):
        sm = _get_sagemaker_client()
        _batch_node_operation(
            "Delete nodes", sm.batch_delete_cluster_nodes, cluster_name, node_ids,
        )

    @hyperpod.command(
        "reboot-nodes", help="Reboot specific nodes",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_HyperpodNodeIdCompleter(with_cwlog=False)),
        ],
    )
    def _reboot_nodes(cluster_name, node_ids):
        sm = _get_sagemaker_client()
        _batch_node_operation(
            "Reboot nodes", sm.batch_reboot_cluster_nodes, cluster_name, node_ids,
        )

    @hyperpod.command(
        "replace-nodes", help="Replace specific nodes",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_HyperpodNodeIdCompleter(with_cwlog=False)),
        ],
    )
    def _replace_nodes(cluster_name, node_ids):
        sm = _get_sagemaker_client()
        _batch_node_operation(
            "Replace nodes", sm.batch_replace_cluster_nodes, cluster_name, node_ids,
        )

    @hyperpod.command(
        "upgrade-ami", help="Update the AMI of a cluster",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("--instance-group-name", metavar="NAME",
                completer=_HyperpodInstanceGroupNameCompleter(),
                help="Instance group to apply update (default: all)"),
            arg("--rolling-update-by", metavar="N|N%",
                help="Number or percentage of instances to update at once"),
        ],
    )
    def _update_software(cluster_name, instance_group_name, rolling_update_by):
        params = {"ClusterName": cluster_name}
        if instance_group_name:
            params["InstanceGroups"] = [{"InstanceGroupName": instance_group_name}]

        if rolling_update_by:
            params["DeploymentConfig"] = {}
            m_count = re.match(r"([0-9]+)$", rolling_update_by)
            m_pct   = re.match(r"([0-9]+)%$", rolling_update_by)
            if m_pct:
                params["DeploymentConfig"]["RollingUpdatePolicy"] = {
                    "MaximumBatchSize": {
                        "Type": "CAPACITY_PERCENTAGE",
                        "Value": int(m_pct.group(1)),
                    }
                }
            elif m_count:
                params["DeploymentConfig"]["RollingUpdatePolicy"] = {
                    "MaximumBatchSize": {
                        "Type": "INSTANCE_COUNT",
                        "Value": int(m_count.group(1)),
                    }
                }
            else:
                print(f"Rolling update parameter incorrectly formatted [{rolling_update_by}]")
                return

        sm = _get_sagemaker_client()
        response = sm.update_cluster_software(**params)
        print(f"Updating cluster software started : {response['ClusterArn']}")

    @hyperpod.command(
        "delete", help="Delete a cluster",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("-y", "--yes", action="store_true",
                help="Skip confirmation"),
        ],
    )
    def _delete(cluster_name, yes):
        if not yes:
            answer = passthrough_input(f"Are you sure deleting the cluster [{cluster_name}]? [y/N] : ")
            if answer.lower() not in ("y", "yes"):
                return

        sm = _get_sagemaker_client()
        try:
            response = sm.delete_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return
        print(f"Deletion started : {response['ClusterArn']}")

    @hyperpod.command(
        "list", help="List clusters in human readable format",
        params=[
            arg("--all-regions", action="store_true",
                help="List clusters in all regions"),
        ],
    )
    def _list(all_regions):
        def _list_one(region_name=None):
            sm = _get_sagemaker_client(region_name=region_name)
            clusters = _list_hyperpod_clusters_all(sm)
            if not clusters:
                return
            name_w   = _max_len(clusters, "ClusterName")
            status_w = _max_len(clusters, "ClusterStatus")
            for cluster in clusters:
                print(
                    f"{cluster['ClusterName']:<{name_w}} : "
                    f"{cluster['ClusterStatus']:<{status_w}} : "
                    f"{cluster['CreationTime'].strftime('%Y/%m/%d %H:%M:%S')} : "
                    f"{cluster['ClusterArn']}"
                )
                if cluster["ClusterStatus"] in ("Failed", "RollingBack"):
                    try:
                        details = sm.describe_cluster(ClusterName=cluster["ClusterName"])
                    except sm.exceptions.ResourceNotFound:
                        print()
                        print("FailureMessage not available.")
                        print()
                        print("---")
                        continue
                    print()
                    for line in details.get("FailureMessage", "").splitlines():
                        print(line)
                    print()
                    print("---")

        if all_regions:
            for region in _hyperpod_regions:
                print(f"[{region}]")
                _list_one(region_name=region)
                print()
        else:
            _list_one()

    @hyperpod.command(
        "describe", help="Describe cluster and its nodes in depth",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("--raw", action="store_true",
                help="Show raw JSON output from boto3 APIs"),
        ],
    )
    def _describe(cluster_name, raw):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        cluster_id = cluster["ClusterArn"].split("/")[-1]
        nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)

        if raw:
            print(json.dumps(cluster, indent=2, default=str))
            print(json.dumps(nodes, indent=2, default=str))
            return

        hostnames = _HyperpodHostnames.instance()
        hostnames.resolve(sm, cluster, nodes)

        print(f"Cluster name : {cluster['ClusterName']}")
        print(f"Cluster Arn : {cluster['ClusterArn']}")
        print(f"Cluster status : {cluster['ClusterStatus']}")
        if cluster.get("FailureMessage"):
            print(f"Failure message : {cluster['FailureMessage']}")
        print()

        max_hostname_len = 0
        for node in nodes:
            hn = hostnames.get_hostname(node["InstanceId"])
            if hn:
                max_hostname_len = max(max_hostname_len, len(hn))

        ig_w   = _max_len(nodes, "InstanceGroupName") if nodes else 0
        stat_w = _max_len(nodes, ("InstanceStatus", "Status")) + 1 if nodes else 0

        for ig in cluster["InstanceGroups"] + cluster["RestrictedInstanceGroups"]:
            print(f"{ig['InstanceGroupName']:<{ig_w}} : {ig['InstanceType']} : "
                  f"{ig['Status']}({ig['CurrentCount']}=>{ig['TargetCount']})")
            for node in nodes:
                if node["InstanceGroupName"] != ig["InstanceGroupName"]:
                    continue

                ig_name = node["InstanceGroupName"]
                node_id = node["InstanceId"]
                hn = hostnames.get_hostname(node_id) or ""
                node_status = node["InstanceStatus"]["Status"]
                ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}"

                if node_status == "Pending":
                    node_status = "*Pending"

                print(f"    {node_id} : {hn:<{max_hostname_len}} : "
                      f"{node_status:<{stat_w}} : "
                      f"{node['LaunchTime'].strftime('%Y/%m/%d %H:%M:%S')} : "
                      f"{ssm_target}")

                msg = node["InstanceStatus"].get("Message")
                if msg:
                    print()
                    for line in msg.splitlines():
                        print(line)
                    print()
                    print("---")

            print()

    @hyperpod.command(
        "wait", help="Wait for asynchronous cluster operations",
        params=[
            arg("cluster_name", nargs="?",
                completer=_HyperpodClusterNameCompleter(),
                help="Cluster name (omit to wait cluster creation/deletion)"),
        ],
    )
    def _wait(cluster_name=None):
        sm = _get_sagemaker_client()
        progress = _ProgressDots()

        if cluster_name is None:
            while True:
                status_list = []
                for cluster in _list_hyperpod_clusters_all(sm):
                    if cluster["ClusterStatus"] not in ("InService", "Failed"):
                        status_list.append(f"{cluster['ClusterName']}:{cluster['ClusterStatus']}")
                progress.tick(", ".join(status_list))
                if not status_list:
                    progress.tick(None)
                    break
                time.sleep(5)
            return

        while True:
            status_list = []
            try:
                cluster = sm.describe_cluster(ClusterName=cluster_name)
            except sm.exceptions.ResourceNotFound:
                print(f"Cluster [{cluster_name}] not found.")
                return

            if cluster["ClusterStatus"] not in ("InService", "Failed"):
                status_list.append(f"{cluster_name}:{cluster['ClusterStatus']}")

            for ig in cluster["InstanceGroups"]:
                ig_name = ig["InstanceGroupName"]
                if ig["Status"] not in ("InService", "Failed"):
                    status_list.append(f"{ig_name}:{ig['Status']}")
                if ig["CurrentCount"] != ig["TargetCount"]:
                    status_list.append(f"{ig_name}:Scaling({ig['CurrentCount']}->{ig['TargetCount']})")

            for node in _list_hyperpod_cluster_nodes_all(sm, cluster_name):
                ig_name = node["InstanceGroupName"]
                node_id = node["InstanceId"]
                node_status = node["InstanceStatus"]["Status"]
                if node_status not in ("Running", "Failed"):
                    status_list.append(f"{ig_name}:{node_id}:{node_status}")

            progress.tick(", ".join(status_list))
            if not status_list:
                progress.tick(None)
                break
            time.sleep(5)

    @hyperpod.command(
        "log", help="Print log from a cluster node",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_id", completer=_HyperpodNodeIdCompleter(with_cwlog=True)),
        ],
    )
    def _log(cluster_name, node_id):
        sm = _get_sagemaker_client()
        logs = _get_boto3_client("logs")

        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        cluster_id = cluster["ClusterArn"].split("/")[-1]
        log_group = f"/aws/sagemaker/Clusters/{cluster_name}/{cluster_id}"

        try:
            streams = _list_hyperpod_log_streams_all(logs, log_group)
        except logs.exceptions.ResourceNotFoundException:
            print(f"Log group [{log_group}] not found.")
            return

        if node_id.startswith("ip-"):
            nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
            hostnames = _HyperpodHostnames.instance()
            hostnames.resolve(sm, cluster, nodes)
            node_id = hostnames.get_node_id(node_id) or node_id

        found = False
        for stream in streams:
            stream_name = stream["logStreamName"]
            if node_id == "*" or stream_name.endswith(node_id):
                header = f"--- {log_group} {stream_name} ---"
                print("-" * len(header))
                print(header)
                print("-" * len(header))
                _print_hyperpod_log(logs, log_group, stream_name)
                print()
                found = True

        if not found:
            print(f"Log stream for [{node_id}] not found.")

    @hyperpod.command(
        "ssm", help="Login to a cluster node with SSM",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_id", completer=_HyperpodNodeIdCompleter(with_cwlog=False)),
        ],
    )
    def _ssm(cluster_name, node_id):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]
        node_id = _resolve_hyperpod_node_id(sm, cluster, cluster_name, node_id)

        ig_name = None
        for node in nodes:
            if node["InstanceId"] == node_id:
                ig_name = node["InstanceGroupName"]
                break
        else:
            print(f"Node ID [{node_id}] not found.")
            return

        ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}"
        passthrough_run(["aws", "ssm", "start-session", "--target", ssm_target])

    @hyperpod.command(
        "ssh",
        help="Install SSH public key on cluster nodes and add Host entries to "
             "~/.ssh/config",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("public_key_file", completer=FileCompleter(),
                help="SSH public key file (e.g. ~/.ssh/id_rsa.pub)"),
            arg("user", nargs="?",
                choices=["ubuntu", "ec2-user"],
                help="Login user. Default: 'ubuntu' for Slurm clusters, "
                     "'ec2-user' for EKS clusters."),
            arg("--instance-group-name", metavar="NAME",
                completer=_HyperpodInstanceGroupNameCompleter(),
                help="Restrict to nodes in this instance group"),
            arg("--node-id", nargs="+", default=[], metavar="NODE",
                completer=_HyperpodNodeIdCompleter(with_cwlog=False),
                help="Restrict to these specific nodes"),
        ],
    )
    def _ssh(cluster_name, public_key_file, user, instance_group_name, node_id):
        try:
            import pexpect  # noqa: F401  (helper imports it; surface the error here)
        except ImportError:
            print("pexpect is required for `awsut hyperpod ssh`. "
                  "Install it: pip install pexpect")
            return

        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        if user is None:
            is_eks = bool(cluster.get("Orchestrator", {}).get("Eks"))
            user = "ec2-user" if is_eks else "ubuntu"

        with open(os.path.expanduser(public_key_file)) as fd:
            public_key = fd.read().strip()
        if len(public_key.splitlines()) > 1:
            print("Public key contains multiple lines unexpectedly.")
            return

        all_nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]

        # Resolve --node-id filter values (strip ig/ prefix, hostname → node_id).
        filter_node_ids: list[str] = []
        if node_id:
            hostnames = _HyperpodHostnames.instance()
            need_hostnames = any(n.startswith("ip-") or "/ip-" in n for n in node_id)
            if need_hostnames:
                try:
                    hostnames.resolve(sm, cluster, all_nodes)
                except Exception:
                    pass
            for n in node_id:
                if "/" in n:
                    n = n.split("/")[-1]
                if n.startswith("ip-"):
                    n = hostnames.get_node_id(n) or n
                filter_node_ids.append(n)

        targets = []
        for node in all_nodes:
            if instance_group_name and node["InstanceGroupName"] != instance_group_name:
                continue
            if filter_node_ids and node["InstanceId"] not in filter_node_ids:
                continue
            targets.append(node)

        if not targets:
            print("No nodes matched the given filters.")
            return

        # 1. Install the public key and capture each node's home directory.
        print_lock = threading.Lock()
        node_homes: dict[str, str] = {}

        def install(node):
            ig_name = node["InstanceGroupName"]
            node_id_local = node["InstanceId"]
            ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id_local}"
            shell_line = (
                f'HOME_DIR=$(getent passwd {user} | cut -d: -f6); '
                f'if [ -z "$HOME_DIR" ]; then echo "HOME_DIR_NOT_FOUND"; exit 1; fi; '
                f'mkdir -p "$HOME_DIR/.ssh" && chmod 700 "$HOME_DIR/.ssh" && '
                f'chown {user} "$HOME_DIR/.ssh" && '
                f'touch "$HOME_DIR/.ssh/authorized_keys" && '
                f'chmod 600 "$HOME_DIR/.ssh/authorized_keys" && '
                f'chown {user} "$HOME_DIR/.ssh/authorized_keys" && '
                f'if ! grep -qF "{public_key}" "$HOME_DIR/.ssh/authorized_keys"; '
                f'then echo "{public_key}" >> "$HOME_DIR/.ssh/authorized_keys"; fi; '
                f'echo "HOME_DIR=$HOME_DIR"'
            )
            output, error = _ssm_run(ssm_target, shell_line, capture=True)
            with print_lock:
                if error:
                    print(f"  [error on {node_id_local}: {error}]")
                    return
                home_match = re.search(r"^HOME_DIR=(\S+)$", output or "", re.MULTILINE)
                if not home_match or "HOME_DIR_NOT_FOUND" in (output or ""):
                    print(f"  [error on {node_id_local}: could not determine home "
                          f"directory for user {user}]")
                    return
                node_homes[node_id_local] = home_match.group(1)
                print(f"Installed SSH key on {ig_name}/{node_id_local} "
                      f"(home: {node_homes[node_id_local]})")

        workers = max(1, min(16, len(targets)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(install, targets):
                pass

        if not node_homes:
            print("Key installation failed on every targeted node; not updating "
                  "~/.ssh/config.")
            return

        # 2. Build the SSH config block(s) and merge into ~/.ssh/config.
        identity_file = public_key_file
        if identity_file.endswith(".pub"):
            identity_file = identity_file[:-4]

        profile = _get_profile()
        region = _get_region() or ""

        config_path = os.path.expanduser("~/.ssh/config")
        existing = ""
        if os.path.exists(config_path):
            with open(config_path) as fd:
                existing = fd.read()

        added_blocks: list[str] = []
        host_aliases: list[str] = []
        for node in targets:
            node_id_local = node["InstanceId"]
            if node_id_local not in node_homes:
                continue
            ig_name = node["InstanceGroupName"]
            host_alias = f"{cluster_name}-{ig_name}-{node_id_local}"
            host_aliases.append(host_alias)

            if re.search(rf"(?m)^Host\s+{re.escape(host_alias)}\s*$", existing):
                continue

            block = (
                f"Host {host_alias}\n"
                f"    HostName sagemaker-cluster:{cluster_id}_{ig_name}-{node_id_local}\n"
                f"    User {user}\n"
                f"    IdentityFile {identity_file}\n"
                f"    ProxyCommand aws --profile {profile} --region {region} "
                f"ssm start-session --target %h --document-name AWS-StartSSHSession "
                f"--parameters portNumber=%p\n"
            )
            added_blocks.append(block)

        if added_blocks:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            sep = "" if not existing or existing.endswith("\n") else "\n"
            with open(config_path, "a") as fd:
                fd.write(sep + "\n" + "\n".join(added_blocks))
            print()
            print(f"Added {len(added_blocks)} Host entr"
                  f"{'y' if len(added_blocks) == 1 else 'ies'} to {config_path}:")
            print()
            for block in added_blocks:
                print(block, end="")
        else:
            print()
            print(f"All Host entries already present in {config_path}.")

        if host_aliases:
            print()
            print("Examples:")
            print(f"  ssh {host_aliases[0]}")
            print(f"  code --remote ssh-remote+{host_aliases[0]} /home/{user}")

    @hyperpod.command(
        "run",
        help="Run a single line command on cluster nodes (in parallel)",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("--instance-group-name", metavar="NAME",
                completer=_HyperpodInstanceGroupNameCompleter(),
                help="Restrict to nodes in this instance group"),
            arg("--instances", nargs="+", default=[], metavar="NODE",
                completer=_HyperpodNodeIdCompleter(with_cwlog=False),
                help="Restrict to these specific nodes"),
            arg("--all", dest="all_nodes", action="store_true",
                help="Run on every node in the cluster (required if neither "
                     "--instance-group-name nor --instances is given)"),
            arg("--max-parallel", type=int, default=16, metavar="N",
                help="Maximum number of nodes to run on concurrently (default: 16)"),
            arg("--command", required=True, metavar="CMD",
                help="Single line of command to run"),
        ],
    )
    def _run(cluster_name, instance_group_name, instances, all_nodes,
             max_parallel, command):
        # Validate targeting first — cheap and useful even without pexpect.
        if not (all_nodes or instance_group_name or instances):
            print("Refusing to run without an explicit target. Pass "
                  "--instance-group-name NAME, --instances NODE [NODE ...], "
                  "or --all to run on every node in the cluster.")
            return
        if all_nodes and (instance_group_name or instances):
            print("--all is mutually exclusive with --instance-group-name "
                  "and --instances.")
            return

        try:
            import pexpect  # noqa: F401  (helper imports it; surface the error here)
        except ImportError:
            print("pexpect is required for `awsut hyperpod run`. "
                  "Install it: pip install pexpect")
            return

        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]

        node_ids: list[str] = []
        for inst in instances:
            if "/" in inst:
                inst = inst.split("/")[-1]
            if inst.startswith("ip-"):
                hostnames = _HyperpodHostnames.instance()
                hostnames.resolve(sm, cluster, nodes)
                inst = hostnames.get_node_id(inst) or inst
            node_ids.append(inst)

        targets = []
        for node in nodes:
            ig_name = node["InstanceGroupName"]
            node_id = node["InstanceId"]
            if instance_group_name and ig_name != instance_group_name:
                continue
            if node_ids and node_id not in node_ids:
                continue
            targets.append(node)

        if not targets:
            print("No nodes matched the given filters.")
            return

        print_lock = threading.Lock()

        def run_on(node):
            ig_name = node["InstanceGroupName"]
            node_id = node["InstanceId"]
            ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}"
            output, error = _ssm_run(ssm_target, command, capture=True)

            with print_lock:
                print(f"--- {ig_name}/{node_id} ---")
                if output:
                    print(output, end="")
                    if not output.endswith("\n"):
                        print()
                if error:
                    print(f"[error running on {node_id}: {error}]")
                print()

        workers = max(1, min(max_parallel, len(targets)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(run_on, targets):
                pass

    @hyperpod.command(
        "search-capacity",
        help="Search Flexible Training Plans offerings in all regions",
        params=[
            arg("--instance-type", required=True, metavar="TYPE",
                completer=ChoiceCompleter(_instance_type_choices),
                help="Instance type (e.g. ml.p5.48xlarge)"),
            arg("--instance-count", required=True, type=int, metavar="N",
                help="Number of instances"),
            arg("--duration-hours", required=True, type=int, metavar="N",
                help="Requested duration in hours"),
        ],
    )
    def _search_capacity(instance_type, instance_count, duration_hours):
        print(f"Searching capacity in {_search_capacity_regions}")
        for region in _search_capacity_regions:
            params = {
                "TargetResources": ["hyperpod-cluster"],
                "InstanceType":    instance_type,
                "InstanceCount":   instance_count,
                "DurationHours":   duration_hours,
            }
            sm = _get_sagemaker_client(region_name=region)
            try:
                response = sm.search_training_plan_offerings(**params)
            except sm.exceptions.ClientError as e:
                if "Invalid instance type" in str(e):
                    continue
                raise

            offerings = response["TrainingPlanOfferings"]
            for tp in offerings:
                for offering in tp["ReservedCapacityOfferings"]:
                    print(
                        f"{offering['AvailabilityZone']:<16} : "
                        f"{offering['DurationHours']:>3}:{offering['DurationMinutes']:<02} : "
                        f"{offering['StartTime']} : {offering['EndTime']}"
                    )
                print("---")

    @hyperpod.command(
        "kubeconfig", help="Update kubeconfig with the EKS cluster",
        params=[arg("cluster_name", completer=_HyperpodClusterNameCompleter())],
    )
    def _kubeconfig(cluster_name):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        try:
            eks_arn = cluster["Orchestrator"]["Eks"]["ClusterArn"]
        except KeyError:
            print("EKS cluster ARN not found in the HyperPod cluster description.")
            return

        eks_name = eks_arn.split("/")[-1]
        passthrough_run(["aws", "eks", "update-kubeconfig", "--name", eks_name])

    @hyperpod.command(
        "events", help="Print historical events",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("--format", choices=["csv", "jsonl"], default="csv",
                help="Output format"),
            arg("--details", action="store_true",
                help="Dump detailed JSON description of each event"),
        ],
    )
    def _events(cluster_name, format, details):
        sm = _get_sagemaker_client()
        try:
            events = _list_hyperpod_cluster_events_all(sm, cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        if details:
            for event in events:
                event_id = event["EventId"]
                try:
                    response = sm.describe_cluster_event(
                        ClusterName=cluster_name, EventId=event_id,
                    )
                    print(json.dumps(response, default=str, indent=2))
                except Exception as e:
                    print(f"Error fetching details for event {event_id}: {e}")
        elif format == "csv":
            print("Timestamp\tResourceType\tInstanceGroup\tInstance\tDescription")
            for event in events:
                print(
                    f"{event['EventTime']}\t"
                    f"{event['ResourceType']}\t"
                    f"{event.get('InstanceGroupName', '')}\t"
                    f"{event.get('InstanceId', '')}\t"
                    f"{event['Description']}"
                )
        elif format == "jsonl":
            for event in events:
                print(json.dumps(event, default=str))


# ─── log exporter (used by `awsut logs export`) ─────────────────────────────

class _LogsExporter:
    def __init__(self, logs_client, log_group, s3_path, start_datetime, end_datetime):
        self.logs_client = logs_client
        self.log_group = log_group
        self.s3_path = s3_path
        self.start_datetime = start_datetime
        self.end_datetime = end_datetime

    def run(self):
        import gzip
        import shutil
        import tempfile

        utcnow = datetime.datetime.utcnow()

        with tempfile.TemporaryDirectory() as export_dir:
            with tempfile.TemporaryDirectory() as plaintext_dir:
                self._export_single_log_group(local_dirname=export_dir)
                self._convert_and_normalize(export_dir, plaintext_dir, gzip)
                self._create_account_info_file(plaintext_dir)
                self._create_zip_file(
                    plaintext_dir,
                    f"./exported_logs_{utcnow.strftime('%Y%m%d_%H%M%S')}",
                    shutil,
                )

    @staticmethod
    def _split_s3_path(s3_path):
        m = re.match(r"s3://([^/]+)/(.*)", s3_path)
        if not m:
            raise ValueError(f"Invalid s3 path: {s3_path}")
        return m.group(1), m.group(2).rstrip("/")

    def _export_single_log_group(self, local_dirname):
        s3_bucket, s3_prefix = self._split_s3_path(self.s3_path)
        start = datetime.datetime.strptime(self.start_datetime, "%Y%m%d_%H%M%S")
        end   = datetime.datetime.strptime(self.end_datetime,   "%Y%m%d_%H%M%S")

        response = self.logs_client.create_export_task(
            logGroupName=self.log_group,
            fromTime=int(start.timestamp() * 1000),
            to=int(end.timestamp() * 1000),
            destination=s3_bucket,
            destinationPrefix=s3_prefix,
        )
        task_id = response["taskId"]

        while True:
            completed = False
            response = self.logs_client.describe_export_tasks(taskId=task_id)
            for task in response["exportTasks"]:
                if task["taskId"] == task_id:
                    code = task["status"]["code"]
                    msg = task["status"].get("message", "")
                    print("Export task status :", code, msg)
                    if code in ("COMPLETED", "CANCELLED", "FAILED"):
                        completed = True
            if completed:
                break
            time.sleep(10)

        s3 = _get_boto3_client("s3")
        exported_prefix = f"{s3_prefix}/{task_id}"
        response = s3.list_objects_v2(Bucket=s3_bucket, Prefix=exported_prefix)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            assert key.startswith(exported_prefix)
            rel = key[len(exported_prefix):].lstrip("/")
            local = os.path.join(local_dirname, rel)
            os.makedirs(os.path.split(local)[0], exist_ok=True)
            print("Downloading", key)
            s3.download_file(Bucket=s3_bucket, Key=key, Filename=local)

    def _convert_and_normalize(self, src, dst, gzip):
        for place, _, files in os.walk(src):
            line_groups: list[list[bytes]] = []
            for filename in files:
                if not filename.endswith(".gz"):
                    continue
                src_filepath = os.path.join(place, filename)
                print("Reading :", src_filepath)
                with gzip.open(src_filepath) as fd:
                    raw = fd.read()
                for line in raw.splitlines():
                    if re.match(
                        rb"[0-9]{4}\-[0-9]{2}\-[0-9]{2}T[0-9]{2}\:[0-9]{2}\:[0-9]{2}\.[0-9]{3}Z .*",
                        line,
                    ):
                        line_groups.append([line])
                    elif line_groups:
                        line_groups[-1].append(line)

            if not line_groups:
                continue
            dst_filepath = os.path.join(
                dst, place[len(src):].lstrip("/\\") + ".log",
            )
            print("Writing", dst_filepath)
            line_groups.sort()
            lines: list[bytes] = []
            for grp in line_groups:
                lines += grp
            data = b"\n".join(lines).replace(b"\0", b"\\0")
            os.makedirs(os.path.split(dst_filepath)[0], exist_ok=True)
            with open(dst_filepath, "wb") as fd:
                fd.write(data)

    def _create_account_info_file(self, dirname):
        sts = _get_boto3_client("sts")
        account_id = sts.get_caller_identity()["Account"]
        region_name = sts.meta.region_name
        with open(os.path.join(dirname, "info.json"), "w") as fd:
            fd.write(json.dumps({
                "account_id":  account_id,
                "region_name": region_name,
            }))

    def _create_zip_file(self, dirname, zip_filename_no_ext, shutil):
        print("Creating a Zip file", zip_filename_no_ext + ".zip")
        shutil.make_archive(zip_filename_no_ext, "zip", dirname)
