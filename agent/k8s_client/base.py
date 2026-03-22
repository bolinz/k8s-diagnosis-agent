from __future__ import annotations

from typing import Protocol

from agent.models import TriggerContext


class KubernetesReadClient(Protocol):
    def list_anomaly_snapshot(self) -> list[dict]:
        ...

    def get_workload_status(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def list_related_pods(self, namespace: str, kind: str, name: str) -> dict:
        ...

    def get_pod_events(self, namespace: str, pod_name: str) -> dict:
        ...

    def get_container_statuses(self, namespace: str, pod_name: str) -> dict:
        ...

    def get_recent_logs(self, namespace: str, pod_name: str, container: str | None = None) -> dict:
        ...

    def get_node_conditions(self, node_name: str | None = None) -> dict:
        ...

    def get_namespace_quotas(self, namespace: str) -> dict:
        ...

    def get_hpa_status(self, namespace: str, target_name: str | None = None) -> dict:
        ...

    def get_resource_metrics(self, namespace: str, name: str | None = None) -> dict:
        ...

    def search_similar_reports(self, trigger: TriggerContext) -> dict:
        ...

    def list_reports(self) -> list[dict]:
        ...

    def get_report(self, name: str) -> dict | None:
        ...

    def watch_events(self):
        ...


class DiagnosisReportWriter(Protocol):
    def upsert_report(self, trigger: TriggerContext, diagnosis: dict) -> dict:
        ...
