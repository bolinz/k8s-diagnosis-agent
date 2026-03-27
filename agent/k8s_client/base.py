from __future__ import annotations

from typing import Protocol

from agent.models import TriggerContext


class KubernetesReadClient(Protocol):
    def list_anomaly_snapshot(self) -> list[dict]:
        ...

    def get_workload_status(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def get_workload_events(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def get_owner_chain(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def get_related_events(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def get_namespace_events(self, namespace: str) -> dict:
        ...

    def list_related_pods(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def get_attached_pvcs(self, namespace: str, pod_name: str) -> dict:
        ...

    def get_pvc_dependents(self, namespace: str, pvc_name: str) -> dict:
        ...

    def get_pod_events(self, namespace: str, pod_name: str) -> dict:
        ...

    def get_pod_spec_summary(self, namespace: str, pod_name: str) -> dict:
        ...

    def get_pod_conditions(self, namespace: str, pod_name: str) -> dict:
        ...

    def get_container_statuses(self, namespace: str, pod_name: str) -> dict:
        ...

    def get_deployment_status(self, namespace: str, name: str) -> dict:
        ...

    def get_replicaset_status(self, namespace: str, name: str) -> dict:
        ...

    def get_config_refs(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def get_pvc_status(
        self,
        namespace: str,
        pvc_name: str | None = None,
        pod_name: str | None = None,
    ) -> dict:
        ...

    def get_recent_logs(self, namespace: str, pod_name: str, container: str | None = None) -> dict:
        ...

    def get_node_conditions(self, node_name: str | None = None) -> dict:
        ...

    def get_node_events(self, node_name: str) -> dict:
        ...

    def get_node_workload_impact(self, node_name: str) -> dict:
        ...

    def get_namespace_quotas(self, namespace: str) -> dict:
        ...

    def get_hpa_status(self, namespace: str, target_name: str | None = None) -> dict:
        ...

    def get_resource_metrics(self, namespace: str, name: str | None = None) -> dict:
        ...

    def search_similar_reports(self, trigger: TriggerContext) -> dict:
        ...

    def get_related_reports(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def list_reports(self) -> list[dict]:
        ...

    def get_report(self, name: str) -> dict | None:
        ...

    def watch_events(self):
        ...


class DiagnosisReportWriter(Protocol):
    def upsert_report(
        self,
        trigger: TriggerContext,
        diagnosis: dict,
        model: str,
        prefix: str,
        category: str = "",
        primary_signal: str = "",
    ) -> dict:
        ...
