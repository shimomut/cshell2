"""SageMaker HyperPod commands ported from the legacy cshell.

Provides the ``hyperpod`` command tree:

* ``hyperpod create``
* ``hyperpod update``
* ``hyperpod scale``
* ``hyperpod delete-nodes``  / ``reboot-nodes`` / ``replace-nodes``
* ``hyperpod update-software``
* ``hyperpod delete``
* ``hyperpod list``
* ``hyperpod describe``
* ``hyperpod wait``
* ``hyperpod log``
* ``hyperpod ssm``
* ``hyperpod ssh print-config`` / ``ssh install-key``
* ``hyperpod run``
* ``hyperpod search-capacity``
* ``hyperpod kubeconfig``
* ``hyperpod events``

Module-level config (override from ``~/.cshell2/config.py``):

    from cshell2.recipes import hyperpod
    hyperpod.hyperpod_endpoint = "https://..."
    hyperpod.sagemaker_service_name = "sagemaker"
    hyperpod.awscli = ["aws"]
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import signal
import subprocess
import time

import boto3

from ..commands import registry as command_registry, arg
from ..completion import (
    ChoiceCompleter,
    Completer,
    Completion,
    CompletionContext,
    FileCompleter,
)


# ─── User-customisable module-level config ──────────────────────────────────

hyperpod_endpoint: str = os.environ.get("HYPERPOD_ENDPOINT", "")
sagemaker_service_name: str = "sagemaker"
awscli: list[str] = ["aws"]

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


# ─── boto3 helpers ──────────────────────────────────────────────────────────

def _get_sagemaker_client(region_name: str | None = None):
    endpoint_url = hyperpod_endpoint or None
    if region_name is None:
        region_name = os.environ.get("AWS_REGION")
    return boto3.client(
        sagemaker_service_name,
        region_name=region_name,
        endpoint_url=endpoint_url,
    )


def _get_logs_client():
    return boto3.client("logs", region_name=os.environ.get("AWS_REGION"))


def _get_eks_client():
    return boto3.client("eks", region_name=os.environ.get("AWS_REGION"))


def _list_clusters_all(sagemaker_client) -> list[dict]:
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


def _list_cluster_nodes_all(sagemaker_client, cluster_name: str) -> list[dict]:
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


def _list_cluster_events_all(sagemaker_client, cluster_name: str) -> list[dict]:
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


def _list_log_streams_all(logs_client, log_group: str) -> list[dict]:
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


# ─── Hostname resolver (cluster InstanceId ↔ short hostname) ───────────────

class _Hostnames:
    _instance: "_Hostnames | None" = None

    @classmethod
    def instance(cls) -> "_Hostnames":
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


# ─── Completers ─────────────────────────────────────────────────────────────

class _ClusterNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            sm = _get_sagemaker_client()
            clusters = _list_clusters_all(sm)
        except Exception:
            return []
        return [Completion(value=c["ClusterName"], description=c.get("ClusterStatus", ""))
                for c in clusters
                if c["ClusterName"].startswith(ctx.prefix)]


class _InstanceGroupNameCompleter(Completer):
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
        choices: list[str] = []
        for ig in cluster.get("InstanceGroups", []):
            choices.append(ig["InstanceGroupName"])
        for ig in cluster.get("RestrictedInstanceGroups", []):
            choices.append(ig["InstanceGroupName"])
        return [Completion(value=c, description="instance group")
                for c in choices if c.startswith(ctx.prefix)]


class _NodeIdCompleter(Completer):
    """Completes node IDs (and IG/node_id, hostname). Reads cluster_name from preceding args."""

    def __init__(self, with_cwlog: bool = False):
        self.with_cwlog = with_cwlog

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        cluster_name = ctx.args[0]
        try:
            sm = _get_sagemaker_client()
            logs = _get_logs_client()
            cluster = sm.describe_cluster(ClusterName=cluster_name)
            nodes = _list_cluster_nodes_all(sm, cluster_name)
        except Exception:
            return []

        hostnames = _Hostnames.instance()
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
                streams = _list_log_streams_all(logs, log_group)
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


def _print_log(logs_client, log_group, stream):
    start_time = int((time.time() - 24 * 60 * 60) * 1000)
    next_token = None
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


def _resolve_node_id(sm, cluster, cluster_name, node_id_input: str) -> str:
    """Strip instance-group prefix and convert hostname to node_id when needed."""
    if "/" in node_id_input:
        node_id_input = node_id_input.split("/")[-1]
    if node_id_input.startswith("ip-"):
        nodes = _list_cluster_nodes_all(sm, cluster_name)
        hostnames = _Hostnames.instance()
        hostnames.resolve(sm, cluster, nodes)
        resolved = hostnames.get_node_id(node_id_input)
        if resolved:
            return resolved
    return node_id_input


# ─── tree definition ────────────────────────────────────────────────────────

def register() -> None:
    hyperpod = command_registry.command("hyperpod", help="HyperPod commands")

    # ── create ──
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
            eks = _get_eks_client()
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

    # ── update ──
    @hyperpod.command(
        "update", help="Update a cluster with JSON file",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter(),
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

    # ── scale ──
    @hyperpod.command(
        "scale", help="Scale up or down an instance group",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("instance_group_name", completer=_InstanceGroupNameCompleter()),
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

        def _drop(d, key):
            d.pop(key, None)

        def _rename(d, old, new):
            if old in d:
                d[new] = d.pop(old)

        if cluster.get("InstanceGroups"):
            params["InstanceGroups"] = []
        for ig in cluster.get("InstanceGroups", []):
            _rename(ig, "TargetCount", "InstanceCount")
            for k in ("CurrentCount", "TargetCount", "Status",
                      "SoftwareUpdateStatus", "TargetStateCount",
                      "ActiveOperations", "FailureMessages",
                      "TrainingPlanStatus", "CurrentImageId"):
                _drop(ig, k)
            _rename(ig, "DesiredImageId", "ImageId")
            if "KubernetesConfig" in ig:
                kc = ig["KubernetesConfig"]
                _rename(kc, "DesiredLabels", "Labels")
                _rename(kc, "DesiredTaints", "Taints")
                _drop(kc, "CurrentLabels")
                _drop(kc, "CurrentTaints")
            if ig["InstanceGroupName"] == instance_group_name:
                ig["InstanceCount"] = target_instance_count
            params["InstanceGroups"].append(ig)

        if cluster.get("RestrictedInstanceGroups"):
            params["RestrictedInstanceGroups"] = []
        for ig in cluster.get("RestrictedInstanceGroups", []):
            _rename(ig, "TargetCount", "InstanceCount")
            for k in ("CurrentCount", "Status", "TrainingPlanStatus"):
                _drop(ig, k)
            if "EnvironmentConfig" in ig:
                _drop(ig["EnvironmentConfig"], "S3OutputPath")
            if ig["InstanceGroupName"] == instance_group_name:
                ig["InstanceCount"] = target_instance_count
            params["RestrictedInstanceGroups"].append(ig)

        response = sm.update_cluster(**params)
        print(f"Updating cluster started : {response['ClusterArn']}")

    # ── batch node operations (delete/reboot/replace) ──
    def _batch_node_operation(operation_name, api, cluster_name, node_ids):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return
        resolved_ids = [
            _resolve_node_id(sm, cluster, cluster_name, n) for n in node_ids
        ]
        response = api(ClusterName=cluster_name, NodeIds=resolved_ids)
        print(f"{operation_name} : {response}")

    @hyperpod.command(
        "delete-nodes", help="Delete specific nodes",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_NodeIdCompleter(with_cwlog=False)),
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
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_NodeIdCompleter(with_cwlog=False)),
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
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_NodeIdCompleter(with_cwlog=False)),
        ],
    )
    def _replace_nodes(cluster_name, node_ids):
        sm = _get_sagemaker_client()
        _batch_node_operation(
            "Replace nodes", sm.batch_replace_cluster_nodes, cluster_name, node_ids,
        )

    # ── update-software ──
    @hyperpod.command(
        "update-software", help="Update the AMI of a cluster",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("--instance-group-name", metavar="NAME",
                completer=_InstanceGroupNameCompleter(),
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

    # ── delete ──
    @hyperpod.command(
        "delete", help="Delete a cluster",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("-y", "--yes", action="store_true",
                help="Skip confirmation"),
        ],
    )
    def _delete(cluster_name, yes):
        if not yes:
            answer = input(f"Are you sure deleting the cluster [{cluster_name}]? [y/N] : ")
            if answer.lower() not in ("y", "yes"):
                return

        sm = _get_sagemaker_client()
        try:
            response = sm.delete_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return
        print(f"Deletion started : {response['ClusterArn']}")

    # ── list ──
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
            clusters = _list_clusters_all(sm)
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

    # ── describe ──
    @hyperpod.command(
        "describe", help="Describe cluster and its nodes in depth",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
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
        nodes = _list_cluster_nodes_all(sm, cluster_name)

        if raw:
            print(json.dumps({"cluster": cluster, "nodes": nodes},
                             indent=2, default=str))
            return

        hostnames = _Hostnames.instance()
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

    # ── wait ──
    @hyperpod.command(
        "wait", help="Wait for asynchronous cluster operations",
        params=[
            arg("cluster_name", nargs="?",
                completer=_ClusterNameCompleter(),
                help="Cluster name (omit to wait cluster creation/deletion)"),
        ],
    )
    def _wait(cluster_name=None):
        sm = _get_sagemaker_client()
        progress = _ProgressDots()

        if cluster_name is None:
            while True:
                status_list = []
                for cluster in _list_clusters_all(sm):
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

            for node in _list_cluster_nodes_all(sm, cluster_name):
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

    # ── log ──
    @hyperpod.command(
        "log", help="Print log from a cluster node",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("node_id", completer=_NodeIdCompleter(with_cwlog=True)),
        ],
    )
    def _log(cluster_name, node_id):
        sm = _get_sagemaker_client()
        logs = _get_logs_client()

        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        cluster_id = cluster["ClusterArn"].split("/")[-1]
        log_group = f"/aws/sagemaker/Clusters/{cluster_name}/{cluster_id}"

        try:
            streams = _list_log_streams_all(logs, log_group)
        except logs.exceptions.ResourceNotFoundException:
            print(f"Log group [{log_group}] not found.")
            return

        if node_id.startswith("ip-"):
            nodes = _list_cluster_nodes_all(sm, cluster_name)
            hostnames = _Hostnames.instance()
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
                _print_log(logs, log_group, stream_name)
                print()
                found = True

        if not found:
            print(f"Log stream for [{node_id}] not found.")

    # ── ssm ──
    @hyperpod.command(
        "ssm", help="Login to a cluster node with SSM",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("node_id", completer=_NodeIdCompleter(with_cwlog=False)),
        ],
    )
    def _ssm(cluster_name, node_id):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        nodes = _list_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]
        node_id = _resolve_node_id(sm, cluster, cluster_name, node_id)

        ig_name = None
        for node in nodes:
            if node["InstanceId"] == node_id:
                ig_name = node["InstanceGroupName"]
                break
        else:
            print(f"Node ID [{node_id}] not found.")
            return

        ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}"
        subprocess.run(["aws", "ssm", "start-session", "--target", ssm_target])

    # ── ssh ──
    ssh = hyperpod.command("ssh", help="Set up SSH access to all cluster nodes")

    @ssh.command(
        "print-config", help="Print SSH config for cluster nodes",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("user", choices=["ubuntu", "ec2-user"]),
        ],
    )
    def _ssh_print_config(cluster_name, user):
        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        nodes = _list_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]
        profile = os.environ.get("AWS_PROFILE", "default")
        region = os.environ.get("AWS_REGION", "")

        for ig in cluster["InstanceGroups"] + cluster["RestrictedInstanceGroups"]:
            node_index = 0
            for node in nodes:
                if node["InstanceGroupName"] != ig["InstanceGroupName"]:
                    continue
                ig_name = node["InstanceGroupName"]
                node_id = node["InstanceId"]
                print()
                print(
                    f"Host {cluster_name}-{ig_name}-{node_index}\n"
                    f"    HostName sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}\n"
                    f"    User {user}\n"
                    f"    IdentityFile ~/keys/842413447717-ec2.pem\n"
                    f"    ProxyCommand aws --profile {profile} --region {region} "
                    f"ssm start-session --target %h --document-name AWS-StartSSHSession "
                    f"--parameters portNumber=%p"
                )
                node_index += 1
        print()

    @ssh.command(
        "install-key", help="Install SSH public key to all cluster nodes",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("home_path",
                help="Path to home directory on the cluster (e.g. /fsx/ubuntu)"),
            arg("public_key_file", completer=FileCompleter(),
                help="SSH public key file"),
        ],
    )
    def _ssh_install_key(cluster_name, home_path, public_key_file):
        try:
            import pexpect
            import pexpect.popen_spawn
        except ImportError:
            print("pexpect is required for `hyperpod ssh install-key`. "
                  "Install it: pip install pexpect")
            return

        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        nodes = _list_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]

        with open(os.path.expanduser(public_key_file)) as fd:
            public_key = fd.read().strip()

        if len(public_key.splitlines()) > 1:
            print("Public key contains multiple lines unexpectedly.")
            return

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            def install(node):
                ig_name = node["InstanceGroupName"]
                node_id = node["InstanceId"]
                ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}"
                authorized_keys_path = os.path.join(home_path, ".ssh/authorized_keys")
                prompt = ["sh-4.2#", "#"]

                print(f"Installing ssh public key to {node_id} {authorized_keys_path}")
                p = pexpect.popen_spawn.PopenSpawn(
                    [*awscli, "ssm", "start-session", "--target", ssm_target]
                )
                p.expect(prompt)
                for line in (
                    f'if ! grep -q "{public_key}" {authorized_keys_path}; then',
                    f"  echo {public_key} >> {authorized_keys_path}",
                    "fi",
                ):
                    p.sendline(line)
                p.expect(prompt)
                p.kill(signal.SIGINT)

            for _ in pool.map(install, nodes):
                pass

    # ── run ──
    @hyperpod.command(
        "run", help="Run a single line command on nodes of an instance group",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("--instance-group-name", metavar="NAME",
                completer=_InstanceGroupNameCompleter(),
                help="Instance group name"),
            arg("--instances", nargs="+", default=[], metavar="NODE",
                completer=_NodeIdCompleter(with_cwlog=False),
                help="Instances to target"),
            arg("--command", required=True, metavar="CMD",
                help="Single line of command to run"),
        ],
    )
    def _run(cluster_name, instance_group_name, instances, command):
        try:
            import pexpect
            import pexpect.popen_spawn
        except ImportError:
            print("pexpect is required for `hyperpod run`. "
                  "Install it: pip install pexpect")
            return

        sm = _get_sagemaker_client()
        try:
            cluster = sm.describe_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            print(f"Cluster [{cluster_name}] not found.")
            return

        nodes = _list_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]

        node_ids: list[str] = []
        for inst in instances:
            if "/" in inst:
                inst = inst.split("/")[-1]
            if inst.startswith("ip-"):
                hostnames = _Hostnames.instance()
                hostnames.resolve(sm, cluster, nodes)
                inst = hostnames.get_node_id(inst) or inst
            node_ids.append(inst)

        custom_prompt = r"pexpect# "

        for node in nodes:
            ig_name = node["InstanceGroupName"]
            node_id = node["InstanceId"]

            if instance_group_name and ig_name != instance_group_name:
                print(f"Skipping {ig_name}/{node_id}")
                continue
            if node_ids and node_id not in node_ids:
                print(f"Skipping {ig_name}/{node_id}")
                continue

            ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}"
            print(f"Running command in {node_id}")
            print()

            p = pexpect.popen_spawn.PopenSpawn(
                [*awscli, "ssm", "start-session", "--target", ssm_target]
            )
            p.expect(["# "])
            print(p.before.decode("utf-8") + p.after.decode("utf-8"), end="")

            p.sendline(f'export PS1="{custom_prompt}"')
            p.expect("\n" + custom_prompt)
            print(p.before.decode("utf-8") + p.after.decode("utf-8"), end="")

            p.sendline(command)
            p.expect(custom_prompt)
            print(p.before.decode("utf-8") + p.after.decode("utf-8"), end="")

            p.kill(signal.SIGINT)
            print()
            print()
            print("---")

    # ── search-capacity ──
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

    # ── kubeconfig ──
    @hyperpod.command(
        "kubeconfig", help="Update kubeconfig with the EKS cluster",
        params=[arg("cluster_name", completer=_ClusterNameCompleter())],
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
        subprocess.run(["aws", "eks", "update-kubeconfig", "--name", eks_name])

    # ── events ──
    @hyperpod.command(
        "events", help="Print historical events",
        params=[
            arg("cluster_name", completer=_ClusterNameCompleter()),
            arg("--format", choices=["csv", "jsonl"], default="csv",
                help="Output format"),
            arg("--details", action="store_true",
                help="Dump detailed JSON description of each event"),
        ],
    )
    def _events(cluster_name, format, details):
        sm = _get_sagemaker_client()
        try:
            events = _list_cluster_events_all(sm, cluster_name)
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
