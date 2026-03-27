from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any
import logging

from agent.metrics import inc_counter
from agent.models import DiagnosisResult, TriggerContext
from agent.runtime_logging import get_logger, log_event

try:
    from kubernetes import client, config
except ImportError:  # pragma: no cover
    client = None
    config = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


LOGGER = get_logger("report_writer")


@dataclass
class DiagnosisReportFormatter:
    analysis_version: str = "0.5.1"

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
        category: str | None = None,
        primary_signal: str | None = None,
    ) -> dict[str, Any]:
        trace = {}
        if isinstance(diagnosis.raw_agent_output, dict):
            candidate = diagnosis.raw_agent_output.get("trace")
            if isinstance(candidate, dict):
                trace = candidate
        model_info = {"name": model, "fallback": diagnosis.used_fallback}
        if trace.get("traceId"):
            model_info["traceId"] = trace["traceId"]
        status = {
            "phase": "Analyzed",
            "severity": diagnosis.severity,
            "summary": diagnosis.summary,
            "probableCauses": diagnosis.probable_causes,
            "evidence": diagnosis.evidence,
            "recommendations": diagnosis.recommendations,
            "confidence": diagnosis.confidence,
            "relatedObjects": diagnosis.related_objects,
            "rootCauseCandidates": diagnosis.root_cause_candidates,
            "evidenceTimeline": diagnosis.evidence_timeline,
            "impactSummary": diagnosis.impact_summary,
            "analysisVersion": self.analysis_version,
            "modelInfo": model_info,
            "rawSignal": raw_signal or {},
            "category": category or "",
            "primarySignal": primary_signal or "",
            "lastAnalyzedAt": _iso(datetime.now(timezone.utc)),
        }
        if trace:
            status["diagnosisTrace"] = trace
        return status

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
        category: str = "",
        primary_signal: str = "",
    ) -> dict[str, Any]:
        name = self.formatter.dedupe_name(trigger, prefix)
        namespace = self.report_namespace
        body = self.formatter.build_spec(name, namespace, trigger)
        status_body = {
            "status": self.formatter.build_status(
                diagnosis,
                model,
                raw_signal=trigger.raw_signal,
                category=category,
                primary_signal=primary_signal,
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
        result = self.custom.patch_namespaced_custom_object_status(
            group="ops.ai.yourorg",
            version="v1alpha1",
            namespace=namespace,
            plural="diagnosisreports",
            name=name,
            body=status_body,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "report_upserted",
            "diagnosis report written",
            report_name=name,
            namespace=trigger.workload.namespace,
            workload_kind=trigger.workload.kind,
            workload_name=trigger.workload.name,
            symptom=trigger.symptom,
        )
        inc_counter("report_upsert_total")
        return result
