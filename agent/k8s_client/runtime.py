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


def _coerce_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {"value": str(value)}


class RuntimeKubernetesClient:
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
        self.custom = client.CustomObjectsApi()

    def list_anomaly_snapshot(self) -> list[dict]:
        findings: list[dict] = []
        pods = self.core.list_pod_for_all_namespaces(watch=False)
        for pod in pods.items:
            namespace = pod.metadata.namespace
            name = pod.metadata.name
            for status in pod.status.container_statuses or []:
                state = getattr(status.state, "waiting", None)
                if state and state.reason in {
                    "CrashLoopBackOff",
                    "ImagePullBackOff",
                    "ErrImagePull",
                    "CreateContainerConfigError",
                }:
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": name,
                            "kind": "Pod",
                            "symptom": state.reason,
                            "observed_for_seconds": 1800,
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
                    }
                )
        return findings

    def get_workload_status(self, namespace: str, kind: str, name: str) -> dict:
        if kind.lower() == "pod":
            return _coerce_dict(self.core.read_namespaced_pod(name, namespace))
        if kind.lower() == "deployment":
            return _coerce_dict(self.apps.read_namespaced_deployment(name, namespace))
        raise ValueError(f"Unsupported workload kind: {kind}")

    def list_related_pods(self, namespace: str, kind: str, name: str) -> dict:
        if kind.lower() == "pod":
            pod = self.core.read_namespaced_pod(name, namespace)
            return {"items": [_coerce_dict(pod)]}
        selector = f"app={name}"
        pods = self.core.list_namespaced_pod(namespace=namespace, label_selector=selector)
        return {"items": [_coerce_dict(item) for item in pods.items], "selector": selector}

    def get_pod_events(self, namespace: str, pod_name: str) -> dict:
        events = self.events.list_namespaced_event(
            namespace=namespace, field_selector=f"involvedObject.name={pod_name}"
        )
        return {"items": [_coerce_dict(item) for item in events.items]}

    def get_container_statuses(self, namespace: str, pod_name: str) -> dict:
        pod = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
        return {"items": [_coerce_dict(item) for item in pod.status.container_statuses or []]}

    def get_recent_logs(
        self, namespace: str, pod_name: str, container: str | None = None
    ) -> dict:
        try:
            text = self.core.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=200,
                timestamps=True,
            )
        except ApiException as exc:
            text = f"log_unavailable: {exc}"
        return {"pod_name": pod_name, "container": container, "logs": text}

    def get_node_conditions(self, node_name: str | None = None) -> dict:
        if node_name:
            node = self.core.read_node(node_name)
            return _coerce_dict(node)
        nodes = self.core.list_node()
        return {"items": [_coerce_dict(item) for item in nodes.items]}

    def get_namespace_quotas(self, namespace: str) -> dict:
        quotas = self.core.list_namespaced_resource_quota(namespace)
        return {"items": [_coerce_dict(item) for item in quotas.items]}

    def get_hpa_status(self, namespace: str, target_name: str | None = None) -> dict:
        hpas = self.autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace)
        items = [_coerce_dict(item) for item in hpas.items]
        if target_name:
            items = [
                item
                for item in items
                if item.get("spec", {})
                .get("scale_target_ref", {})
                .get("name")
                == target_name
            ]
        return {"items": items}

    def get_resource_metrics(self, namespace: str, name: str | None = None) -> dict:
        return {
            "status": "unavailable",
            "reason": "metrics integration is cluster-specific and should be wired through a dedicated adapter",
            "namespace": namespace,
            "name": name,
        }

    def search_similar_reports(self, trigger: TriggerContext) -> dict:
        reports = self.custom.list_namespaced_custom_object(
            group="ops.ai.yourorg",
            version="v1alpha1",
            namespace=self.report_namespace,
            plural="diagnosisreports",
        )
        items = []
        for item in reports.get("items", []):
            spec = item.get("spec", {})
            if (
                spec.get("namespace") == trigger.workload.namespace
                and spec.get("workloadRef", {}).get("name") == trigger.workload.name
                and spec.get("symptom") == trigger.symptom
            ):
                items.append(item)
        return {"items": items}

    def list_reports(self) -> list[dict]:
        reports = self.custom.list_namespaced_custom_object(
            group="ops.ai.yourorg",
            version="v1alpha1",
            namespace=self.report_namespace,
            plural="diagnosisreports",
        )
        return reports.get("items", [])

    def get_report(self, name: str) -> dict | None:
        try:
            return self.custom.get_namespaced_custom_object(
                group="ops.ai.yourorg",
                version="v1alpha1",
                namespace=self.report_namespace,
                plural="diagnosisreports",
                name=name,
            )
        except Exception:
            return None

    def watch_events(self):
        if Watch is None:
            raise RuntimeError("kubernetes watch dependency is required")
        watcher = Watch()
        return watcher.stream(self.core.list_event_for_all_namespaces, timeout_seconds=0)
