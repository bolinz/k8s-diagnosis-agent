from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.analyzers.rules import RuleEngine
from agent.config.settings import Settings
from agent.k8s_client.base import KubernetesReadClient
from agent.models import DiagnosisResult, PendingFinding, TriggerContext, WorkloadRef
from agent.orchestrator.codex_agent import CodexDiagnosisAgent
from agent.reporting.diagnosis_reporter import (
    DiagnosisReportFormatter,
    KubernetesDiagnosisReportWriter,
)
from agent.tools.registry import ToolRegistry


@dataclass
class AgentService:
    settings: Settings
    client: KubernetesReadClient
    codex_agent: CodexDiagnosisAgent
    report_writer: object | None = None
    formatter: DiagnosisReportFormatter = field(default_factory=DiagnosisReportFormatter)
    recent_events: dict[str, datetime] = field(default_factory=dict)

    def scan_once(self) -> list[dict]:
        findings = self._collect_findings()
        results = []
        for finding in findings:
            results.append(self.process_trigger(finding.trigger))
        return results

    def process_alert(self, payload: dict) -> dict:
        namespace = payload.get("namespace", "default")
        name = payload.get("name", payload.get("pod_name", "unknown"))
        kind = payload.get("kind", "Pod")
        trigger = TriggerContext(
            source="alert",
            cluster=self.settings.cluster_name,
            workload=WorkloadRef(kind=kind, namespace=namespace, name=name),
            symptom=payload.get("symptom", "Pending"),
            observed_for_seconds=int(payload.get("observed_for_seconds", 0)),
            raw_signal=payload,
        )
        return self.process_trigger(trigger)

    def process_trigger(self, trigger: TriggerContext) -> dict:
        registry = ToolRegistry(self.client, trigger)
        diagnosis = self._ensure_complete_diagnosis(
            trigger, self.codex_agent.diagnose(trigger, registry)
        )
        writer = self.report_writer
        if writer is None:
            return {
                **self.formatter.build_spec(
                    self.formatter.dedupe_name(
                        trigger, self.settings.diagnosis_name_prefix
                    ),
                    self.settings.report_namespace,
                    trigger,
                ),
                "status": self.formatter.build_status(
                    diagnosis,
                    self.settings.openai_model,
                    raw_signal=trigger.raw_signal,
                ),
            }
        return writer.upsert_report(
            trigger,
            diagnosis,
            self.settings.openai_model,
            self.settings.diagnosis_name_prefix,
        )

    def process_event_trigger(self, trigger: TriggerContext) -> dict | None:
        key = ":".join(
            [
                trigger.workload.namespace,
                trigger.workload.kind,
                trigger.workload.name,
                trigger.symptom,
            ]
        )
        now = datetime.now(timezone.utc)
        seen = self.recent_events.get(key)
        if seen is not None:
            delta = (now - seen).total_seconds()
            if delta < self.settings.event_dedupe_window_seconds:
                return None
        self.recent_events[key] = now
        return self.process_trigger(trigger)

    def list_reports(self, params: dict | None = None) -> list[dict]:
        params = params or {}
        namespace_filter = (params.get("namespace") or [""])[0]
        severity_filter = (params.get("severity") or [""])[0]
        symptom_filter = (params.get("symptom") or [""])[0]
        items = [self._normalize_report(item) for item in self.client.list_reports()]
        items.sort(key=lambda item: item.get("lastAnalyzedAt", ""), reverse=True)
        filtered = []
        for item in items:
            if namespace_filter and namespace_filter not in item["namespace"]:
                continue
            if severity_filter and severity_filter != item["severity"]:
                continue
            if symptom_filter and symptom_filter not in item["symptom"]:
                continue
            filtered.append(item)
        return filtered

    def get_report(self, name: str) -> dict | None:
        report = self.client.get_report(name)
        if report is None:
            return None
        return self._normalize_report(report)

    def backfill_incomplete_reports(self) -> list[dict]:
        updated = []
        if self.report_writer is None:
            return updated
        for report in self.client.list_reports():
            status = report.get("status", {})
            if status.get("summary") and status.get("evidence"):
                continue
            trigger = self._trigger_from_report(report)
            diagnosis = self._ensure_complete_diagnosis(
                trigger,
                self.codex_agent.rule_engine.fallback_diagnosis(trigger),
            )
            updated.append(
                self.report_writer.upsert_report(
                    trigger,
                    diagnosis,
                    self.settings.openai_model,
                    self.settings.diagnosis_name_prefix,
                )
            )
        return updated

    def _collect_findings(self) -> list[PendingFinding]:
        engine = self.codex_agent.rule_engine
        return engine.findings_from_snapshot(self.client.list_anomaly_snapshot())

    def _normalize_report(self, report: dict) -> dict:
        metadata = report.get("metadata", {})
        spec = report.get("spec", {})
        status = report.get("status", {})
        workload = spec.get("workloadRef", {})
        return {
            "name": metadata.get("name", ""),
            "namespace": spec.get("namespace", ""),
            "source": spec.get("source", ""),
            "cluster": spec.get("cluster", ""),
            "symptom": spec.get("symptom", ""),
            "observedFor": spec.get("observedFor", 0),
            "triggerAt": spec.get("triggerAt", ""),
            "workload": {
                "kind": workload.get("kind", ""),
                "name": workload.get("name", ""),
            },
            "severity": status.get("severity", "warning"),
            "summary": status.get("summary", ""),
            "probableCauses": status.get("probableCauses", []),
            "evidence": status.get("evidence", []),
            "recommendations": status.get("recommendations", []),
            "confidence": status.get("confidence", 0.0),
            "lastAnalyzedAt": status.get("lastAnalyzedAt", ""),
            "analysisVersion": status.get("analysisVersion", ""),
            "modelInfo": status.get("modelInfo", {}),
            "rawSignal": self._raw_signal_summary(status.get("rawSignal", {})),
        }

    def _ensure_complete_diagnosis(
        self,
        trigger: TriggerContext,
        diagnosis: DiagnosisResult,
    ) -> DiagnosisResult:
        fallback = self.codex_agent.rule_engine.fallback_diagnosis(trigger)
        summary = diagnosis.summary.strip() if diagnosis.summary else ""
        if not summary or summary == "Diagnosis incomplete":
            summary = self._fallback_summary(trigger)
        return DiagnosisResult(
            summary=summary,
            severity=diagnosis.severity or fallback.severity,
            probable_causes=diagnosis.probable_causes or fallback.probable_causes,
            evidence=diagnosis.evidence or self._fallback_evidence(trigger),
            recommendations=diagnosis.recommendations or fallback.recommendations,
            confidence=diagnosis.confidence if diagnosis.confidence > 0 else fallback.confidence,
            raw_agent_output=diagnosis.raw_agent_output,
            used_fallback=diagnosis.used_fallback
            or not diagnosis.summary
            or not diagnosis.evidence
            or not diagnosis.recommendations,
        )

    def _fallback_summary(self, trigger: TriggerContext) -> str:
        summary = (
            f"Detected {trigger.symptom} for "
            f"{trigger.workload.kind}/{trigger.workload.name} in {trigger.workload.namespace}."
        )
        reason = trigger.raw_signal.get("reason")
        if trigger.source == "event" and reason:
            summary += f" Triggered by Kubernetes event {reason}."
        return summary

    def _fallback_evidence(self, trigger: TriggerContext) -> list[str]:
        evidence = [
            f"symptom={trigger.symptom}",
            f"source={trigger.source}",
            f"workload={trigger.workload.kind}/{trigger.workload.name}",
            f"namespace={trigger.workload.namespace}",
        ]
        if trigger.observed_for_seconds:
            evidence.append(f"observed_for_seconds={trigger.observed_for_seconds}")
        for key in ("eventType", "reason", "message", "timestamp"):
            value = trigger.raw_signal.get(key)
            if value:
                evidence.append(f"{key}={value}")
        return evidence

    def _trigger_from_report(self, report: dict) -> TriggerContext:
        spec = report.get("spec", {})
        workload = spec.get("workloadRef", {})
        status = report.get("status", {})
        return TriggerContext(
            source=spec.get("source", "scheduled"),
            cluster=spec.get("cluster", self.settings.cluster_name),
            workload=WorkloadRef(
                kind=workload.get("kind", "Pod"),
                namespace=spec.get("namespace", "default"),
                name=workload.get("name", "unknown"),
            ),
            symptom=spec.get("symptom", "Pending"),
            observed_for_seconds=int(spec.get("observedFor", 0)),
            raw_signal=status.get("rawSignal", {}),
        )

    def _raw_signal_summary(self, raw_signal: dict) -> dict:
        if not isinstance(raw_signal, dict):
            return {}
        summary = {}
        for key in ("eventType", "reason", "message", "timestamp"):
            value = raw_signal.get(key)
            if value:
                summary[key] = value
        return summary


def build_runtime_service(settings: Settings) -> AgentService:
    from agent.k8s_client.runtime import RuntimeKubernetesClient
    from agent.orchestrator.responses_client import OpenAIResponsesClient

    client = RuntimeKubernetesClient(report_namespace=settings.report_namespace)
    responses_client = OpenAIResponsesClient(
        api_key=settings.openai_api_key or "",
        model=settings.openai_model,
        api_base_url=settings.api_base_url,
        timeout_seconds=settings.request_timeout_seconds,
    )
    engine = RuleEngine(
        cluster_name=settings.cluster_name,
        min_observation_seconds=settings.min_observation_seconds,
    )
    codex_agent = CodexDiagnosisAgent(
        responses_client=responses_client,
        rule_engine=engine,
        model=settings.openai_model,
        max_tool_calls=settings.max_tool_calls,
        max_input_bytes=settings.max_input_bytes,
    )
    writer = KubernetesDiagnosisReportWriter(
        report_namespace=settings.report_namespace,
        formatter=DiagnosisReportFormatter(),
    )
    service = AgentService(
        settings=settings,
        client=client,
        codex_agent=codex_agent,
        report_writer=writer,
    )
    service.backfill_incomplete_reports()
    return service
