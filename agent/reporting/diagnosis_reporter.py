from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from agent.models import DiagnosisResult, TriggerContext

try:
    from kubernetes import client, config
except ImportError:  # pragma: no cover
    client = None
    config = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class DiagnosisReportFormatter:
    analysis_version: str = "0.1.0"

    def build_spec(
        self,
        report_name: str,
        report_namespace: str,
        trigger: TriggerContext,
    ) -> dict[str, Any]:
        return {
            "apiVersion": "ops.ai.yourorg/v1alpha1",
            "kind": "DiagnosisReport",
            "metadata": {
                "name": report_name,
                "namespace": report_namespace,
            },
            "spec": {
                "source": trigger.source,
                "cluster": trigger.cluster,
                "namespace": trigger.workload.namespace,
                "workloadRef": {
                    "kind": trigger.workload.kind,
                    "name": trigger.workload.name,
                },
                "symptom": trigger.symptom,
                "observedFor": trigger.observed_for_seconds,
                "triggerAt": _iso(trigger.trigger_at),
            },
        }

    def build_status(
        self,
        diagnosis: DiagnosisResult,
        model: str,
        raw_signal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "phase": "Analyzed",
            "severity": diagnosis.severity,
            "summary": diagnosis.summary,
            "probableCauses": diagnosis.probable_causes,
            "evidence": diagnosis.evidence,
            "recommendations": diagnosis.recommendations,
            "confidence": diagnosis.confidence,
            "analysisVersion": self.analysis_version,
            "modelInfo": {"name": model, "fallback": diagnosis.used_fallback},
            "rawSignal": raw_signal or {},
            "lastAnalyzedAt": _iso(datetime.now(timezone.utc)),
        }

    def dedupe_name(self, trigger: TriggerContext, prefix: str) -> str:
        digest = sha1(
            f"{trigger.workload.namespace}:{trigger.workload.kind}:{trigger.workload.name}:{trigger.symptom}".encode(
                "utf-8"
            )
        ).hexdigest()[:10]
        return f"{prefix}-{digest}"


class KubernetesDiagnosisReportWriter:
    def __init__(self, report_namespace: str, formatter: DiagnosisReportFormatter) -> None:
        if client is None or config is None:
            raise RuntimeError(
                "The kubernetes dependency is required for the report writer."
            )
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self.report_namespace = report_namespace
        self.formatter = formatter
        self.custom = client.CustomObjectsApi()

    def upsert_report(
        self,
        trigger: TriggerContext,
        diagnosis: DiagnosisResult,
        model: str,
        prefix: str,
    ) -> dict[str, Any]:
        name = self.formatter.dedupe_name(trigger, prefix)
        namespace = self.report_namespace
        body = self.formatter.build_spec(name, namespace, trigger)
        status_body = {
            "status": self.formatter.build_status(
                diagnosis,
                model,
                raw_signal=trigger.raw_signal,
            )
        }
        try:
            self.custom.get_namespaced_custom_object(
                group="ops.ai.yourorg",
                version="v1alpha1",
                namespace=namespace,
                plural="diagnosisreports",
                name=name,
            )
            self.custom.patch_namespaced_custom_object(
                group="ops.ai.yourorg",
                version="v1alpha1",
                namespace=namespace,
                plural="diagnosisreports",
                name=name,
                body=body,
            )
        except Exception:
            self.custom.create_namespaced_custom_object(
                group="ops.ai.yourorg",
                version="v1alpha1",
                namespace=namespace,
                plural="diagnosisreports",
                body=body,
            )
        return self.custom.patch_namespaced_custom_object_status(
            group="ops.ai.yourorg",
            version="v1alpha1",
            namespace=namespace,
            plural="diagnosisreports",
            name=name,
            body=status_body,
        )
