"""AWS utility commands ported from the legacy cshell.

Provides the ``awsut`` command tree:

* ``awsut console <page>``           — open a Management Console URL
* ``awsut recent-cost``              — show recent AWS cost
* ``awsut ec2 list|start|stop|reboot``
* ``awsut logs list|monitor|export``
* ``awsut cloudformation list|watch|open``
* ``awsut sagemaker jobs list|describe|watch|stop``
* ``awsut sagemaker hub hubs|list|versions|describe|files|trace``
* ``awsut sagemaker studio domains|spaces|apps|profiles|logs|start|url|stop``
* ``awsut sagemaker hyperpod create|update|scale|add-ig|remove-ig|
  delete-nodes|reboot-nodes|replace-nodes|upgrade-ami|delete|
  list|describe|watch|log|ssm|ssh|run|search-capacity|
  kubeconfig|events``

The ``sagemaker`` group lives in the ``_awsut_sagemaker`` subpackage (this
module is already long enough); it is attached from :func:`register` because
``CommandRegistry`` roots cannot be re-opened from a second module.

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

import datetime
import fnmatch
import json
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from typing import Callable

import boto3

from ..commands import registry as command_registry, arg
from ..completion import Completer, Completion, CompletionContext
from ..completion_cache import aws_env_key, get_or_fetch
from ..variables import Var, registry as var_registry


# ─── User-customisable module-level config ──────────────────────────────────
#
# Override from ~/.cshell2/config.py:
#
#     from cshell2.recipes import awsut
#     awsut.console_pages = {...}
#     awsut.console_url_modifier_func = lambda account, role, url: ...

console_pages: dict[str, str] = {
    "home":           "https://console.aws.amazon.com/console/home",
    "s3":             "https://console.aws.amazon.com/s3/home",
    "iam":            "https://console.aws.amazon.com/iam/home",
    "cloudformation": "https://console.aws.amazon.com/cloudformation/home",
    "cost":           "https://console.aws.amazon.com/costmanagement/home#/cost-explorer?granularity=Daily&historicalRelativeRange=LAST_7_DAYS",
    "sagemaker":      "https://console.aws.amazon.com/sagemaker/home",
    "hyperpod":       "https://console.aws.amazon.com/sagemaker/home#/cluster-management",
}

console_url_modifier_func: Callable[[str, str, str], str] | None = None

awscli: list[str] = ["aws"]

# Set via ``var sagemaker_endpoint=...`` / ``var sagemaker_service_name=...``
# at the prompt.  Module-level Python state (not env vars) so they don't
# leak into subprocesses.
sagemaker_endpoint: str = ""
sagemaker_service_name: str = "sagemaker"


# ─── boto3 / AWS helpers ────────────────────────────────────────────────────

def _get_boto3_client(service_name: str):
    region_name = os.environ.get("AWS_REGION")
    return boto3.Session().client(service_name, region_name=region_name)


def _get_sagemaker_client(region_name: str | None = None):
    endpoint_url = sagemaker_endpoint or None
    if region_name is None:
        region_name = os.environ.get("AWS_REGION")
    return boto3.Session().client(
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


def _print_json(obj) -> None:
    """Print JSON, syntax-highlighted when stdout is a TTY."""
    text = json.dumps(obj, indent=2, default=str)
    if sys.stdout.isatty():
        try:
            from pygments import highlight
            from pygments.formatters import Terminal256Formatter
            from pygments.lexers import JsonLexer
            sys.stdout.write(
                highlight(text, JsonLexer(), Terminal256Formatter(style="monokai"))
            )
            return
        except Exception:
            pass
    print(text)


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


# ─── progress dots used by `cloudformation watch` ───────────────────────────

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
        try:
            values = get_or_fetch(
                ("awsut.cost_services", aws_env_key()),
                self._fetch,
            )
        except Exception:
            return []
        return [Completion(value=v) for v in values if v.startswith(ctx.prefix)]

    @staticmethod
    def _fetch() -> list[str]:
        today = datetime.datetime.now().date()
        client = _get_boto3_client("ce")
        response = client.get_dimension_values(
            TimePeriod={
                "Start": (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d"),
                "End":   (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
            },
            Dimension="SERVICE",
        )
        return [d["Value"] for d in response.get("DimensionValues", [])]


class _Ec2InstanceNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            names = get_or_fetch(
                ("awsut.ec2_instance_names", aws_env_key()),
                self._fetch,
            )
        except Exception:
            return []
        return [Completion(value=n, description="ec2 instance")
                for n in names if n.startswith(ctx.prefix)]

    @staticmethod
    def _fetch() -> list[str]:
        ec2 = _get_boto3_client("ec2")
        response = ec2.describe_instances()
        names: list[str] = []
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                for tag in instance.get("Tags", []) or []:
                    if tag["Key"] == "Name":
                        names.append(tag["Value"])
                        break
        return names


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
        # ``_list_log_groups`` takes a server-side prefix filter — using
        # ``ctx.prefix`` directly would defeat caching (every keystroke is a
        # new key). Cache the unfiltered list per profile/region instead and
        # filter locally; the keystroke rate is low enough that the larger
        # initial fetch is the right tradeoff.
        try:
            groups = get_or_fetch(
                ("awsut.log_groups", aws_env_key()),
                lambda: _list_log_groups(""),
            )
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
            streams = get_or_fetch(
                ("awsut.log_streams", aws_env_key(), group_name),
                lambda: _get_boto3_client("logs")
                            .describe_log_streams(logGroupName=group_name)
                            .get("logStreams", []),
            )
        except Exception:
            return []
        return [Completion(value=s["logStreamName"], description="log stream")
                for s in streams
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


def _osc8(url: str, label: str) -> str:
    """Wrap ``label`` in an OSC 8 hyperlink escape so terminals that support
    it (including the VSCode integrated terminal) render a clickable link.
    Terminals that don't support OSC 8 simply show ``label`` (the URL), which
    most terminals auto-link anyway."""
    return f"\x1b]8;;{url}\x1b\\{label}\x1b]8;;\x1b\\"


def _cf_console_url(stack_arn: str) -> str:
    """Build the CloudFormation console URL for a stack's events view,
    applying ``console_url_modifier_func`` when configured."""
    region = _get_region() or "us-east-1"
    encoded = urllib.parse.quote(stack_arn)
    url = (f"https://{region}.console.aws.amazon.com/cloudformation/home"
           f"?region={region}#/stacks/events?stackId={encoded}")
    if console_url_modifier_func is not None:
        profile_name = _get_profile()
        profile = _get_all_profiles().get(profile_name, {})
        url = console_url_modifier_func(
            profile.get("account", ""), profile.get("role", ""), url,
        )
    return url


def _cf_failure_reasons(cf_client, stack_name: str) -> list[str]:
    """Return the resource-level failure reasons for a stack's most recent
    operation, taken from ``describe_stack_events`` (most-recent page)."""
    try:
        response = cf_client.describe_stack_events(StackName=stack_name)
    except Exception:                       # best-effort — never break the watch
        return []
    reasons: list[str] = []
    for event in response.get("StackEvents", []):
        if not event.get("ResourceStatus", "").endswith("_FAILED"):
            continue
        reason = event.get("ResourceStatusReason")
        if not reason:
            continue
        line = f"{event.get('LogicalResourceId', '')}: {reason}"
        if line not in reasons:
            reasons.append(line)
    return reasons


def _report_cf_failure(cf_client, stack: dict) -> None:
    """Print a failed stack's status, failure reason(s) and a clickable
    console hyperlink."""
    name = stack["StackName"]
    print(f"{name}: {stack['StackStatus']}")
    reason = stack.get("StackStatusReason")
    if reason:
        print(f"  {reason}")
    for line in _cf_failure_reasons(cf_client, name):
        print(f"  {line}")
    url = _cf_console_url(stack.get("StackId", name))
    print(f"  Console: {_osc8(url, url)}")


class _CfStackNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            stacks = get_or_fetch(
                ("awsut.cf_stacks", aws_env_key()),
                lambda: _list_cf_stacks(
                    _get_boto3_client("cloudformation"),
                    include_deleted=False,
                    include_successfully_completed=True,
                    include_nested=True,
                ),
            )
        except Exception:
            return []
        return [Completion(value=s["StackName"], description=s["StackStatus"])
                for s in stacks if s["StackName"].startswith(ctx.prefix)]


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

    # Lazy: the subpackage does `from .. import awsut`, so it can only be
    # imported once this module is fully loaded.
    from ._awsut_sagemaker import register_sagemaker
    register_sagemaker(awsut)

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
    cf = awsut.command("cloudformation", help="CloudFormation commands")

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

    @cf.command("watch", help="Watch CloudFormation operations until they finish")
    def _cf_watch():
        cf_client = _get_boto3_client("cloudformation")
        progress = _ProgressDots()
        stacks: list[dict] = []
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

        # Report any stacks that ended in a failed state (e.g. DELETE_FAILED),
        # including the failure reason and a clickable console hyperlink.
        failed = [s for s in stacks if s["StackStatus"].endswith("_FAILED")]
        for stack in failed:
            _report_cf_failure(cf_client, stack)

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
