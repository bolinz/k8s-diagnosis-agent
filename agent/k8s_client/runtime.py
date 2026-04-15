from __future__ import annotations

from typing import Any

from agent.models import TriggerContext

try:
    from kubernetes import client, config
    from kubernetes.client import ApiException
    from kubernetes.watch import Watch
except ImportError:  # pragma: no cover - exercised only when dependency missing
    client = None
    config = None
    ApiException = Exception
    Watch = None

from agent.k8s_client.executor import ToolExecutor


POD_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "ContainerCannotRun",
}


class RuntimeKubernetesClient:
    """Kubernetes runtime client acting as a facade over ToolExecutor.

    Initializes Kubernetes API clients and delegates all tool operations
    to ToolExecutor. Non-tool operations (list_anomaly_snapshot, watch_events)
    are implemented directly.
    """

    def __init__(self, report_namespace: str) -> None:
        if client is None or config is None:
            raise RuntimeError(
                "The kubernetes dependency is required for the runtime client."
            )
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self.report_namespace = report_namespace
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.events = client.CoreV1Api()
        self.autoscaling = client.AutoscalingV2Api()
        self.policy = client.PolicyV1Api()
        self.custom = client.CustomObjectsApi()
        self._executor = ToolExecutor(
            core=self.core,
            apps=self.apps,
            events=self.events,
            autoscaling=self.autoscaling,
            policy=self.policy,
            custom=self.custom,
            report_namespace=report_namespace,
        )

    def execute(self, tool_name: str, kwargs: dict[str, Any]) -> Any:
        """Delegate tool execution to ToolExecutor."""
        method = getattr(self._executor, tool_name, None)
        if method is None:
            raise AttributeError(f"ToolExecutor has no method '{tool_name}'")
        return method(**kwargs)

    def list_anomaly_snapshot(self) -> list[dict]:
        findings: list[dict] = []
        pods = self.core.list_pod_for_all_namespaces(watch=False)
        for pod in pods.items:
            namespace = pod.metadata.namespace
            name = pod.metadata.name
            for status in pod.status.container_statuses or []:
                waiting = getattr(status.state, "waiting", None)
                if waiting and waiting.reason in POD_WAITING_REASONS:
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": name,
                            "kind": "Pod",
                            "symptom": waiting.reason,
                            "observed_for_seconds": 1800,
                            "containerReason": waiting.reason,
                        }
                    )
                terminated = getattr(status.state, "terminated", None)
                if terminated and terminated.reason == "OOMKilled":
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": name,
                            "kind": "Pod",
                            "symptom": "OOMKilled",
                            "observed_for_seconds": 1800,
                            "containerReason": terminated.reason,
                        }
                    )
            if pod.status.phase == "Pending":
                findings.append(
                    {
                        "namespace": namespace,
                        "name": name,
                        "kind": "Pod",
                        "symptom": "Pending",
                        "observed_for_seconds": 1800,
                        "podPhase": pod.status.phase,
                        "podReason": pod.status.reason,
                    }
                )
            if pod.status.reason == "Evicted":
                findings.append(
                    {
                        "namespace": namespace,
                        "name": name,
                        "kind": "Pod",
                        "symptom": "Evicted",
                        "observed_for_seconds": 1800,
                        "podPhase": pod.status.phase,
                        "podReason": pod.status.reason,
                    }
                )

        deployments = self.apps.list_deployment_for_all_namespaces(watch=False)
        for deployment in deployments.items:
            namespace = deployment.metadata.namespace
            name = deployment.metadata.name
            desired = int(getattr(deployment.spec, "replicas", 0) or 0)
            available = int(getattr(deployment.status, "available_replicas", 0) or 0)
            unavailable = int(getattr(deployment.status, "unavailable_replicas", 0) or 0)
            for condition in deployment.status.conditions or []:
                if (
                    getattr(condition, "type", "") == "Progressing"
                    and getattr(condition, "reason", "") == "ProgressDeadlineExceeded"
                ):
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": name,
                            "kind": "Deployment",
                            "symptom": "ProgressDeadlineExceeded",
                            "observed_for_seconds": 1800,
                            "deploymentCondition": getattr(condition, "reason", ""),
                        }
                    )
            if desired > 0 and (available < desired or unavailable > 0):
                findings.append(
                    {
                        "namespace": namespace,
                        "name": name,
                        "kind": "Deployment",
                        "symptom": "ReplicaMismatch",
                        "observed_for_seconds": 1800,
                        "deploymentCondition": "ReplicaMismatch",
                    }
                )

        # PVC Pending: pods whose attached PVC is not Bound
        pvcs = self.core.list_persistent_volume_claim_for_all_namespaces(watch=False)
        pending_pvc_map: dict[tuple[str, str], str] = {}
        for pvc in pvcs.items:
            phase = getattr(pvc.status, "phase", None)
            if phase != "Bound":
                pending_pvc_map[(pvc.metadata.namespace, pvc.metadata.name)] = phase or "Pending"
        for pod in pods.items:
            namespace = pod.metadata.namespace
            for volume in pod.spec.volumes or []:
                claim = getattr(volume, "persistent_volume_claim", None)
                if claim is None:
                    continue
                pvc_name = getattr(claim, "claim_name", "")
                pvc_key = (namespace, pvc_name)
                if pvc_key in pending_pvc_map:
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": pod.metadata.name,
                            "kind": "Pod",
                            "symptom": "PVCPending",
                            "observed_for_seconds": 1800,
                            "message": f"pvc {pvc_name} is {pending_pvc_map[pvc_key]}",
                        }
                    )

        # HPA at maxReplicas
        hpas = self.autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces(watch=False)
        for hpa in hpas.items:
            namespace = hpa.metadata.namespace
            name = hpa.metadata.name
            current_replicas = int(getattr(hpa.status, "current_replicas", 0) or 0)
            max_replicas = int(getattr(hpa.spec, "max_replicas", 0) or 0)
            if max_replicas > 0 and current_replicas >= max_replicas:
                findings.append(
                    {
                        "namespace": namespace,
                        "name": name,
                        "kind": "HorizontalPodAutoscaler",
                        "symptom": "HPAMaxReplicas",
                        "observed_for_seconds": 1800,
                        "message": f"hpa at maxReplicas={max_replicas}, current={current_replicas}",
                    }
                )

        # PDB DisruptionBlocked: pods blocked by unsatisfied PDB
        try:
            pdbs = self.policy.list_pod_disruption_budget_for_all_namespaces(watch=False)
        except Exception:
            pdbs = []
        for pdb in pdbs or []:
            namespace = pdb.metadata.namespace
            name = pdb.metadata.name
            if hasattr(pdb.status, "disruptions_allowed"):
                disruptions_allowed = int(getattr(pdb.status, "disruptions_allowed", 1) or 1)
                if disruptions_allowed == 0:
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": name,
                            "kind": "PodDisruptionBudget",
                            "symptom": "PDBDisruptionBlocked",
                            "observed_for_seconds": 1800,
                            "message": "pdb disruptions_allowed=0",
                        }
                    )

        # ResourceQuota exceeded: check namespace quota events
        namespaces = self.core.list_namespace(watch=False)
        for ns in namespaces.items:
            ns_name = ns.metadata.name
            try:
                events = self.events.list_namespaced_event(
                    namespace=ns_name,
                    field_selector="reason=FailedCreate",
                )
                for event in events.items or []:
                    msg = getattr(event, "message", "") or ""
                    if "exceeded" in msg.lower() or "quota" in msg.lower():
                        findings.append(
                            {
                                "namespace": ns_name,
                                "name": ns_name,
                                "kind": "Namespace",
                                "symptom": "ResourceQuotaExceeded",
                                "observed_for_seconds": 1800,
                                "message": msg,
                            }
                        )
                        break
            except Exception:
                pass

        return findings

    def watch_events(self):
        if Watch is None:
            raise RuntimeError("kubernetes watch dependency is required")
        watcher = Watch()
        return watcher.stream(self.core.list_event_for_all_namespaces, timeout_seconds=0)

    # -------------------------------------------------------------------------
    # Tool methods - delegate to ToolExecutor for backward compatibility
    # -------------------------------------------------------------------------

    def get_workload_status(self, namespace: str, kind: str, name: str) -> dict:
        return self._executor.get_workload_status(namespace, kind, name)

    def get_workload_events(self, namespace: str, kind: str, name: str) -> dict:
        return self._executor.get_workload_events(namespace, kind, name)

    def get_owner_chain(self, namespace: str, kind: str, name: str) -> dict:
        return self._executor.get_owner_chain(namespace, kind, name)

    def get_related_events(self, namespace: str, kind: str, name: str) -> dict:
        return self._executor.get_related_events(namespace, kind, name)

    def get_namespace_events(self, namespace: str) -> dict:
        return self._executor.get_namespace_events(namespace)

    def list_related_pods(self, namespace: str, kind: str, name: str) -> dict:
        return self._executor.list_related_pods(namespace, kind, name)

    def get_attached_pvcs(self, namespace: str, pod_name: str) -> dict:
        return self._executor.get_attached_pvcs(namespace, pod_name)

    def get_pvc_dependents(self, namespace: str, pvc_name: str) -> dict:
        return self._executor.get_pvc_dependents(namespace, pvc_name)

    def get_pod_events(self, namespace: str, pod_name: str) -> dict:
        return self._executor.get_pod_events(namespace, pod_name)

    def get_pod_spec_summary(self, namespace: str, pod_name: str) -> dict:
        return self._executor.get_pod_spec_summary(namespace, pod_name)

    def get_pod_conditions(self, namespace: str, pod_name: str) -> dict:
        return self._executor.get_pod_conditions(namespace, pod_name)

    def get_container_statuses(self, namespace: str, pod_name: str) -> dict:
        return self._executor.get_container_statuses(namespace, pod_name)

    def get_deployment_status(self, namespace: str, name: str) -> dict:
        return self._executor.get_deployment_status(namespace, name)

    def get_replicaset_status(self, namespace: str, name: str) -> dict:
        return self._executor.get_replicaset_status(namespace, name)

    def get_config_refs(self, namespace: str, kind: str, name: str) -> dict:
        return self._executor.get_config_refs(namespace, kind, name)

    def get_pvc_status(self, namespace: str, pvc_name: str | None = None, pod_name: str | None = None) -> dict:
        return self._executor.get_pvc_status(namespace, pvc_name, pod_name)

    def get_recent_logs(self, namespace: str, pod_name: str, container: str | None = None) -> dict:
        return self._executor.get_recent_logs(namespace, pod_name, container)

    def get_node_conditions(self, node_name: str | None = None) -> dict:
        return self._executor.get_node_conditions(node_name)

    def get_node_events(self, node_name: str) -> dict:
        return self._executor.get_node_events(node_name)

    def get_node_workload_impact(self, node_name: str) -> dict:
        return self._executor.get_node_workload_impact(node_name)

    def get_namespace_quotas(self, namespace: str) -> dict:
        return self._executor.get_namespace_quotas(namespace)

    def get_hpa_status(self, namespace: str, target_name: str | None = None) -> dict:
        return self._executor.get_hpa_status(namespace, target_name)

    def get_resource_metrics(self, namespace: str, name: str | None = None) -> dict:
        return self._executor.get_resource_metrics(namespace, name)

    def search_similar_reports(self, trigger: TriggerContext) -> dict:
        return self._executor.search_similar_reports(trigger)

    def get_related_reports(self, namespace: str, kind: str, name: str) -> dict:
        return self._executor.get_related_reports(namespace, kind, name)

    def list_reports(self) -> list[dict]:
        return self._executor.list_reports()

    def get_report(self, name: str) -> dict | None:
        return self._executor.get_report(name)
