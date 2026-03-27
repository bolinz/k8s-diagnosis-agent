from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
import logging
from typing import Any

from agent.analyzers.attribution import score_root_cause_candidates
from agent.analyzers.rules import RuleEngine
from agent.config.settings import Settings
from agent.k8s_client.base import KubernetesReadClient
from agent.metrics import inc_counter, observe_diagnosis_duration
from agent.models import DiagnosisResult, PendingFinding, TriggerContext, WorkloadRef
from agent.orchestrator.diagnosis_agent import DiagnosisAgent
from agent.orchestrator.responses_client import (
    ModelClient,
    OllamaResponsesClient,
    OpenAIResponsesClient,
)
from agent.reporting.diagnosis_reporter import DiagnosisReportFormatter, KubernetesDiagnosisReportWriter
from agent.runtime_logging import get_logger, log_event
from agent.tools.registry import ToolRegistry


LOGGER = get_logger("agent_service")


@dataclass
class AgentService:
    settings: Settings
    client: KubernetesReadClient
    codex_agent: DiagnosisAgent
    report_writer: object | None = None
    formatter: DiagnosisReportFormatter = field(default_factory=DiagnosisReportFormatter)
    recent_events: dict[str, datetime] = field(default_factory=dict)
    recent_event_storms: dict[str, dict[str, Any]] = field(default_factory=dict)

    def scan_once(self) -> list[dict]:
        log_event(LOGGER, logging.INFO, "scan_start", "scheduled scan started")
        findings = self._collect_findings()
        results = []
        for finding in findings:
            results.append(self.process_trigger(finding.trigger))
        log_event(
            LOGGER,
            logging.INFO,
            "scan_end",
            "scheduled scan finished",
            findings=len(findings),
            reports=len(results),
        )
        return results

    def process_alert(self, payload: dict) -> dict:
        log_event(
            LOGGER,
            logging.INFO,
            "alert_received",
            "alert webhook received",
            namespace=payload.get("namespace", "default"),
            workload_name=payload.get("name", payload.get("pod_name", "unknown")),
            workload_kind=payload.get("kind", "Pod"),
            symptom=payload.get("symptom", "Pending"),
        )
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
        start = perf_counter()
        inc_counter("diagnosis_requests_total")
        trigger = self._normalize_trigger(trigger)
        trigger = self._augment_trigger_signal(trigger)
        trigger = self._attach_correlation_context(trigger)
        log_event(
            LOGGER,
            logging.INFO,
            "trigger_processing",
            "processing trigger",
            source=trigger.source,
            namespace=trigger.workload.namespace,
            workload_kind=trigger.workload.kind,
            workload_name=trigger.workload.name,
            symptom=trigger.symptom,
        )
        registry = ToolRegistry(
            self.client,
            trigger,
            scope_mode=self.settings.scope_mode,
            allowed_namespaces=set(self.settings.scope_allowed_namespaces),
        )
        diagnosis = self._ensure_complete_diagnosis(
            trigger,
            self.codex_agent.diagnose(trigger, registry),
        )
        if diagnosis.used_fallback:
            inc_counter("diagnosis_fallback_total")
        result = self._persist_report(trigger, diagnosis)
        observe_diagnosis_duration(perf_counter() - start)
        return result

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
        state = self._next_event_storm_state(key, now)
        count = int(state.get("count", 0))
        threshold = max(2, self.settings.event_storm_threshold)
        if count == 1:
            self.recent_events[key] = now
            return self.process_trigger(trigger)
        if count < threshold:
            log_event(
                LOGGER,
                logging.INFO,
                "event_deduped",
                "skipping duplicate event trigger",
                namespace=trigger.workload.namespace,
                workload_kind=trigger.workload.kind,
                workload_name=trigger.workload.name,
                symptom=trigger.symptom,
                burst_count=count,
                burst_threshold=threshold,
            )
            return None
        if not state.get("aggregated", False):
            state["aggregated"] = True
            log_event(
                LOGGER,
                logging.WARNING,
                "event_storm_aggregated",
                "aggregating event storm into one fallback report",
                namespace=trigger.workload.namespace,
                workload_kind=trigger.workload.kind,
                workload_name=trigger.workload.name,
                symptom=trigger.symptom,
                burst_count=count,
                burst_threshold=threshold,
            )
            return self._process_aggregated_event_trigger(trigger, state)
        log_event(
            LOGGER,
            logging.INFO,
            "event_storm_suppressed",
            "suppressing additional event storm triggers",
            namespace=trigger.workload.namespace,
            workload_kind=trigger.workload.kind,
            workload_name=trigger.workload.name,
            symptom=trigger.symptom,
            burst_count=count,
            burst_threshold=threshold,
        )
        return None

    def _next_event_storm_state(self, key: str, now: datetime) -> dict[str, Any]:
        window = max(1, self.settings.event_dedupe_window_seconds)
        state = self.recent_event_storms.get(key)
        if state is None:
            state = {"first_seen": now, "last_seen": now, "count": 0, "aggregated": False}
        else:
            first_seen = state.get("first_seen")
            if not isinstance(first_seen, datetime) or (now - first_seen).total_seconds() >= window:
                state = {"first_seen": now, "last_seen": now, "count": 0, "aggregated": False}
            else:
                state["last_seen"] = now
        state["count"] = int(state.get("count", 0)) + 1
        state["last_seen"] = now
        self.recent_event_storms[key] = state
        return state

    def _process_aggregated_event_trigger(self, trigger: TriggerContext, state: dict[str, Any]) -> dict:
        first_seen = state.get("first_seen")
        last_seen = state.get("last_seen")
        if isinstance(first_seen, datetime) and isinstance(last_seen, datetime):
            window_seconds = max(1, int((last_seen - first_seen).total_seconds()))
        else:
            window_seconds = max(1, self.settings.event_dedupe_window_seconds)
        aggregated_raw_signal = dict(trigger.raw_signal or {})
        aggregated_raw_signal.update(
            {
                "aggregated": True,
                "stormCount": int(state.get("count", 0)),
                "stormWindowSeconds": window_seconds,
            }
        )
        aggregated_trigger = TriggerContext(
            source=trigger.source,
            cluster=trigger.cluster,
            workload=trigger.workload,
            symptom=trigger.symptom,
            observed_for_seconds=max(trigger.observed_for_seconds, window_seconds),
            trigger_at=trigger.trigger_at,
            raw_signal=aggregated_raw_signal,
            correlation_context=trigger.correlation_context,
        )
        aggregated_trigger = self._normalize_trigger(aggregated_trigger)
        aggregated_trigger = self._augment_trigger_signal(aggregated_trigger)
        aggregated_trigger = self._attach_correlation_context(aggregated_trigger)
        diagnosis = self._ensure_complete_diagnosis(
            aggregated_trigger,
            self.codex_agent.rule_engine.fallback_diagnosis(aggregated_trigger),
        )
        return self._persist_report(aggregated_trigger, diagnosis)

    def _persist_report(self, trigger: TriggerContext, diagnosis: DiagnosisResult) -> dict:
        category = self._category_for_symptom(trigger.symptom)
        primary_signal = self._derive_primary_signal(trigger.symptom, trigger.raw_signal)
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
                    self._active_model_name(),
                    raw_signal=trigger.raw_signal,
                    category=category,
                    primary_signal=primary_signal,
                ),
            }
        return writer.upsert_report(
            trigger,
            diagnosis,
            self._active_model_name(),
            self.settings.diagnosis_name_prefix,
            category=category,
            primary_signal=primary_signal,
        )

    def list_reports(self, params: dict | None = None) -> list[dict]:
        params = params or {}
        namespace_filter = (params.get("namespace") or [""])[0]
        severity_filter = (params.get("severity") or [""])[0]
        symptom_filter = (params.get("symptom") or [""])[0]
        category_filter = (params.get("category") or [""])[0]
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
            if category_filter and category_filter != item["category"]:
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
                    self._active_model_name(),
                    self.settings.diagnosis_name_prefix,
                )
            )
        return updated

    def _collect_findings(self) -> list[PendingFinding]:
        engine = self.codex_agent.rule_engine
        findings = engine.findings_from_snapshot(self.client.list_anomaly_snapshot())
        return [
            finding
            for finding in findings
            if not self._is_self_workload(
                finding.trigger.workload.namespace,
                finding.trigger.workload.name,
            )
        ]

    def _normalize_report(self, report: dict) -> dict:
        metadata = report.get("metadata", {})
        spec = report.get("spec", {})
        status = report.get("status", {})
        spec = self._normalize_report_spec(spec, status.get("rawSignal", {}))
        status = self._normalize_report_status(status)
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
            "relatedObjects": status.get("relatedObjects", []),
            "rootCauseCandidates": status.get("rootCauseCandidates", []),
            "evidenceTimeline": status.get("evidenceTimeline", []),
            "impactSummary": status.get("impactSummary", {}),
            "lastAnalyzedAt": status.get("lastAnalyzedAt", ""),
            "analysisVersion": status.get("analysisVersion", ""),
            "modelInfo": status.get("modelInfo", {}),
            "diagnosisTrace": status.get("diagnosisTrace", {}),
            "rawSignal": self._raw_signal_summary(status.get("rawSignal", {})),
            "category": self._normalize_category(
                status.get("category", ""),
                spec.get("symptom", ""),
            ),
            "primarySignal": self._normalize_primary_signal(
                status.get("primarySignal", ""),
                status.get("rawSignal", {}),
                spec.get("symptom", ""),
            ),
        }

    def _is_self_workload(self, namespace: str, name: str) -> bool:
        if namespace != self.settings.report_namespace:
            return False
        return bool(name) and (
            name == self.settings.workload_name
            or name.startswith(f"{self.settings.workload_name}-")
        )

    def _ensure_complete_diagnosis(
        self,
        trigger: TriggerContext,
        diagnosis: DiagnosisResult,
    ) -> DiagnosisResult:
        fallback = self.codex_agent.rule_engine.fallback_diagnosis(trigger)
        correlation = trigger.correlation_context or {}
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
            related_objects=diagnosis.related_objects or correlation.get("relatedObjects", []),
            root_cause_candidates=diagnosis.root_cause_candidates
            or correlation.get("rootCauseCandidates", []),
            evidence_timeline=diagnosis.evidence_timeline
            or correlation.get("evidenceTimeline", []),
            impact_summary=diagnosis.impact_summary or correlation.get("impactSummary", {}),
            raw_agent_output=diagnosis.raw_agent_output,
            used_fallback=diagnosis.used_fallback
            or not diagnosis.summary
            or not diagnosis.evidence
            or not diagnosis.recommendations,
        )

    def _normalize_report_spec(self, spec: dict[str, Any], raw_signal: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(spec)
        workload = dict(normalized.get("workloadRef", {}))
        involved = raw_signal.get("involvedObject", {}) if isinstance(raw_signal, dict) else {}
        kind = workload.get("kind") or involved.get("kind") or "Pod"
        name = workload.get("name") or involved.get("name") or ""
        namespace = (
            normalized.get("namespace")
            or involved.get("namespace")
            or "default"
        )
        normalized["cluster"] = normalized.get("cluster") or self.settings.cluster_name
        normalized["namespace"] = namespace
        normalized["workloadRef"] = {
            "kind": kind,
            "name": name,
        }
        return normalized

    def _normalize_report_status(self, status: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(status)
        model_info = normalized.get("modelInfo")
        if not isinstance(model_info, dict):
            model_info = {}
        normalized["modelInfo"] = {
            "name": model_info.get("name") or self._active_model_name(),
            "fallback": bool(model_info.get("fallback", False)),
        }
        if model_info.get("traceId"):
            normalized["modelInfo"]["traceId"] = str(model_info.get("traceId"))
        raw_signal = normalized.get("rawSignal")
        normalized["rawSignal"] = raw_signal if isinstance(raw_signal, dict) else {}
        trace = normalized.get("diagnosisTrace")
        normalized["diagnosisTrace"] = trace if isinstance(trace, dict) else {}
        normalized["relatedObjects"] = [
            item
            for item in normalized.get("relatedObjects", [])
            if isinstance(item, dict)
        ]
        normalized["rootCauseCandidates"] = [
            item
            for item in normalized.get("rootCauseCandidates", [])
            if isinstance(item, dict)
        ]
        normalized["evidenceTimeline"] = [
            item
            for item in normalized.get("evidenceTimeline", [])
            if isinstance(item, dict)
        ]
        normalized["category"] = (
            str(normalized.get("category", "")).strip() if normalized.get("category") is not None else ""
        )
        normalized["primarySignal"] = (
            str(normalized.get("primarySignal", "")).strip()
            if normalized.get("primarySignal") is not None
            else ""
        )
        impact = normalized.get("impactSummary", {})
        normalized["impactSummary"] = impact if isinstance(impact, dict) else {}
        return normalized

    def _normalize_category(self, category: str, symptom: str) -> str:
        if category:
            return category
        return self._category_for_symptom(symptom)

    def _normalize_primary_signal(
        self,
        primary_signal: str,
        raw_signal: dict[str, Any],
        symptom: str,
    ) -> str:
        if primary_signal:
            return primary_signal
        return self._derive_primary_signal(
            symptom=symptom,
            raw_signal=raw_signal if isinstance(raw_signal, dict) else {},
        )

    def _active_model_name(self) -> str:
        model = getattr(self.codex_agent, "model", "")
        if isinstance(model, str) and model:
            return model
        return self.settings.openai_model

    def _category_for_symptom(self, symptom: str) -> str:
        if symptom in {"ImagePullBackOff", "ErrImagePull"}:
            return "image"
        if symptom in {"Pending", "FailedMount", "Evicted", "NodeNotReadyImpact"}:
            return "scheduling"
        if symptom in {"CrashLoopBackOff", "OOMKilled", "ProbeFailure", "ContainerCannotRun"}:
            return "runtime"
        if symptom in {"ProgressDeadlineExceeded", "ReplicaMismatch"}:
            return "rollout"
        if symptom in {"CreateContainerConfigError", "CreateContainerError", "FailedCreatePodSandbox"}:
            return "configuration"
        return "general"

    def _derive_primary_signal(self, symptom: str, raw_signal: dict[str, Any]) -> str:
        for key in (
            "reason",
            "containerReason",
            "deploymentCondition",
            "podReason",
            "podPhase",
            "pvcPhase",
            "eventType",
        ):
            value = raw_signal.get(key)
            if value:
                return str(value)
        return symptom

    def _augment_trigger_signal(self, trigger: TriggerContext) -> TriggerContext:
        raw_signal = dict(trigger.raw_signal)
        try:
            if trigger.workload.kind.lower() == "pod" and trigger.workload.name:
                pod_conditions = self.client.get_pod_conditions(
                    namespace=trigger.workload.namespace,
                    pod_name=trigger.workload.name,
                )
                raw_signal["podPhase"] = raw_signal.get("podPhase") or pod_conditions.get("phase")
                raw_signal["podReason"] = raw_signal.get("podReason") or pod_conditions.get("reason")
                statuses = self.client.get_container_statuses(
                    namespace=trigger.workload.namespace,
                    pod_name=trigger.workload.name,
                )
                for item in statuses.get("items", []):
                    state = item.get("state", {})
                    waiting = state.get("waiting", {})
                    terminated = state.get("terminated", {})
                    reason = waiting.get("reason") or terminated.get("reason")
                    if reason:
                        raw_signal["containerReason"] = raw_signal.get("containerReason") or reason
                        break
                if trigger.symptom == "FailedMount":
                    pvc_status = self.client.get_pvc_status(
                        namespace=trigger.workload.namespace,
                        pod_name=trigger.workload.name,
                    )
                    items = pvc_status.get("items", [])
                    if items:
                        raw_signal["pvcPhase"] = raw_signal.get("pvcPhase") or items[0].get("phase")
            elif trigger.workload.kind.lower() == "deployment" and trigger.workload.name:
                deployment_status = self.client.get_deployment_status(
                    namespace=trigger.workload.namespace,
                    name=trigger.workload.name,
                )
                conditions = deployment_status.get("conditions", [])
                if conditions:
                    first_condition = conditions[0]
                    raw_signal["deploymentCondition"] = (
                        raw_signal.get("deploymentCondition")
                        or first_condition.get("reason")
                        or first_condition.get("type")
                    )
        except Exception:
            return trigger
        return TriggerContext(
            source=trigger.source,
            cluster=trigger.cluster,
            workload=trigger.workload,
            symptom=trigger.symptom,
            observed_for_seconds=trigger.observed_for_seconds,
            trigger_at=trigger.trigger_at,
            raw_signal=raw_signal,
            correlation_context=trigger.correlation_context,
        )

    def _attach_correlation_context(self, trigger: TriggerContext) -> TriggerContext:
        return TriggerContext(
            source=trigger.source,
            cluster=trigger.cluster,
            workload=trigger.workload,
            symptom=trigger.symptom,
            observed_for_seconds=trigger.observed_for_seconds,
            trigger_at=trigger.trigger_at,
            raw_signal=trigger.raw_signal,
            correlation_context=self._build_correlation_context(trigger),
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
        for key in ("podPhase", "podReason", "containerReason", "deploymentCondition", "pvcPhase"):
            value = trigger.raw_signal.get(key)
            if value:
                evidence.append(f"{key}={value}")
        return evidence

    def _normalize_trigger(self, trigger: TriggerContext) -> TriggerContext:
        raw_signal = self._normalize_event_signal(trigger)
        involved = raw_signal.get("involvedObject", {}) if trigger.source == "event" else {}
        workload_name = trigger.workload.name or involved.get("name") or ""
        workload_kind = trigger.workload.kind or involved.get("kind") or "Pod"
        workload_namespace = (
            trigger.workload.namespace
            or involved.get("namespace")
            or "default"
        )
        return TriggerContext(
            source=trigger.source,
            cluster=trigger.cluster or self.settings.cluster_name,
            workload=WorkloadRef(
                kind=workload_kind,
                namespace=workload_namespace,
                name=workload_name,
            ),
            symptom=trigger.symptom,
            observed_for_seconds=trigger.observed_for_seconds,
            trigger_at=trigger.trigger_at,
            raw_signal=raw_signal,
            correlation_context=trigger.correlation_context,
        )

    def _normalize_event_signal(self, trigger: TriggerContext) -> dict[str, Any]:
        raw_signal = dict(trigger.raw_signal) if isinstance(trigger.raw_signal, dict) else {}
        if trigger.source != "event":
            return raw_signal
        involved = raw_signal.get("involvedObject")
        if not isinstance(involved, dict):
            involved = {}
        involved = {
            "kind": involved.get("kind") or trigger.workload.kind or "Pod",
            "name": involved.get("name") or trigger.workload.name or "",
            "namespace": involved.get("namespace") or trigger.workload.namespace or "default",
        }
        raw_signal["involvedObject"] = involved
        if trigger.raw_signal.get("eventType"):
            raw_signal["eventType"] = trigger.raw_signal.get("eventType")
        if trigger.raw_signal.get("reason"):
            raw_signal["reason"] = trigger.raw_signal.get("reason")
        if trigger.raw_signal.get("message"):
            raw_signal["message"] = trigger.raw_signal.get("message")
        raw_signal["timestamp"] = raw_signal.get("timestamp") or trigger.trigger_at.astimezone(timezone.utc).isoformat()
        return raw_signal

    def _trigger_from_report(self, report: dict) -> TriggerContext:
        spec = report.get("spec", {})
        workload = spec.get("workloadRef", {})
        status = report.get("status", {})
        return self._normalize_trigger(TriggerContext(
            source=spec.get("source", "scheduled"),
            cluster=spec.get("cluster", self.settings.cluster_name),
            workload=WorkloadRef(
                kind=workload.get("kind", "Pod"),
                namespace=spec.get("namespace", "default"),
                name=workload.get("name", ""),
            ),
            symptom=spec.get("symptom", "Pending"),
            observed_for_seconds=int(spec.get("observedFor", 0)),
            raw_signal=status.get("rawSignal", {}),
            correlation_context={
                "relatedObjects": status.get("relatedObjects", []),
                "rootCauseCandidates": status.get("rootCauseCandidates", []),
                "evidenceTimeline": status.get("evidenceTimeline", []),
                "impactSummary": status.get("impactSummary", {}),
            },
        ))

    def _raw_signal_summary(self, raw_signal: dict) -> dict:
        if not isinstance(raw_signal, dict):
            return {}
        summary = {}
        for key in (
            "eventType",
            "reason",
            "message",
            "timestamp",
            "podPhase",
            "podReason",
            "containerReason",
            "deploymentCondition",
            "pvcPhase",
        ):
            value = raw_signal.get(key)
            if value:
                summary[key] = value
        return summary

    def _build_correlation_context(self, trigger: TriggerContext) -> dict[str, Any]:
        related_objects: list[dict[str, Any]] = []
        root_candidates: list[dict[str, Any]] = []
        evidence_timeline: list[dict[str, Any]] = []

        self._append_related_object(
            related_objects,
            trigger.workload.kind,
            trigger.workload.namespace,
            trigger.workload.name,
            "primary",
        )

        if trigger.workload.kind.lower() == "pod":
            self._collect_pod_correlation(trigger, related_objects, root_candidates, evidence_timeline)
        else:
            self._collect_workload_correlation(trigger, related_objects, root_candidates, evidence_timeline)

        reports = self.client.get_related_reports(
            namespace=trigger.workload.namespace,
            kind=trigger.workload.kind,
            name=trigger.workload.name,
        )
        report_items = reports.get("items", []) if isinstance(reports, dict) else []
        impact_summary = {
            "workloadCount": len(
                {
                    (
                        item.get("namespace", ""),
                        item.get("kind", ""),
                        item.get("name", ""),
                    )
                    for item in related_objects
                    if item.get("kind") not in {"PVC", "Node", "DiagnosisReport"}
                }
            ),
            "podCount": len([item for item in related_objects if item.get("kind") == "Pod"]),
            "crossNamespace": len({item.get("namespace", "") for item in related_objects if item.get("namespace")}) > 1,
            "relatedReportCount": len(report_items),
        }
        root_candidates = self._prioritize_root_candidates(trigger.symptom, root_candidates)
        evidence_timeline.sort(key=lambda item: item.get("time", ""))
        return {
            "relatedObjects": related_objects,
            "rootCauseCandidates": root_candidates[:3],
            "evidenceTimeline": evidence_timeline[:10],
            "impactSummary": impact_summary,
        }

    def _collect_pod_correlation(
        self,
        trigger: TriggerContext,
        related_objects: list[dict[str, Any]],
        root_candidates: list[dict[str, Any]],
        evidence_timeline: list[dict[str, Any]],
    ) -> None:
        owner_chain = self.client.get_owner_chain(
            namespace=trigger.workload.namespace,
            kind=trigger.workload.kind,
            name=trigger.workload.name,
        )
        owners = owner_chain.get("items", []) if isinstance(owner_chain, dict) else []
        for item in owners[1:]:
            self._append_related_object(
                related_objects,
                item.get("kind", ""),
                item.get("namespace", trigger.workload.namespace),
                item.get("name", ""),
                "owner",
            )

        events = self.client.get_related_events(
            namespace=trigger.workload.namespace,
            kind="Pod",
            name=trigger.workload.name,
        )
        self._append_timeline(evidence_timeline, events.get("items", []), trigger.workload)

        spec = self.client.get_pod_spec_summary(
            namespace=trigger.workload.namespace,
            pod_name=trigger.workload.name,
        )
        node_name = spec.get("nodeName") if isinstance(spec, dict) else None
        if node_name:
            self._append_related_object(related_objects, "Node", "", node_name, "upstream-suspect")
            impact = self.client.get_node_workload_impact(node_name=node_name)
            if impact.get("podCount", 0) > 1:
                self._append_root_candidate(
                    root_candidates,
                    "Node",
                    "",
                    node_name,
                    f"Node {node_name} currently affects multiple pods on the same host.",
                    0.72,
                )

        pvcs = self.client.get_attached_pvcs(
            namespace=trigger.workload.namespace,
            pod_name=trigger.workload.name,
        )
        for item in pvcs.get("items", []) if isinstance(pvcs, dict) else []:
            self._append_related_object(
                related_objects,
                "PVC",
                trigger.workload.namespace,
                item.get("name", ""),
                "upstream-suspect" if trigger.symptom == "FailedMount" else "affected",
            )
            if item.get("phase") and item.get("phase") != "Bound":
                self._append_root_candidate(
                    root_candidates,
                    "PVC",
                    trigger.workload.namespace,
                    item.get("name", ""),
                    f"PVC {item.get('name', '')} is {item.get('phase')} and may block pod startup.",
                    0.85,
                )
                dependents = self.client.get_pvc_dependents(
                    namespace=trigger.workload.namespace,
                    pvc_name=item.get("name", ""),
                )
                for dependent in dependents.get("items", []) if isinstance(dependents, dict) else []:
                    self._append_related_object(
                        related_objects,
                        dependent.get("kind", "Pod"),
                        dependent.get("namespace", trigger.workload.namespace),
                        dependent.get("name", ""),
                        "affected",
                    )

        if trigger.symptom in {
            "ImagePullBackOff",
            "ErrImagePull",
            "CreateContainerConfigError",
            "ContainerCannotRun",
        }:
            owner = next(
                (item for item in owners if item.get("kind") in {"Deployment", "ReplicaSet", "StatefulSet", "DaemonSet"}),
                None,
            )
            if owner:
                pods = self.client.list_related_pods(
                    namespace=trigger.workload.namespace,
                    kind=owner.get("kind", ""),
                    name=owner.get("name", ""),
                )
                pod_items = pods.get("items", []) if isinstance(pods, dict) else []
                if len(pod_items) > 1:
                    self._append_root_candidate(
                        root_candidates,
                        owner.get("kind", ""),
                        trigger.workload.namespace,
                        owner.get("name", ""),
                        f"Multiple pods owned by {owner.get('kind', '')}/{owner.get('name', '')} show the same startup failure pattern.",
                        0.78,
                    )
                    for pod in pod_items:
                        metadata = pod.get("metadata", {})
                        self._append_related_object(
                            related_objects,
                            "Pod",
                            trigger.workload.namespace,
                            metadata.get("name", ""),
                            "affected",
                        )

    def _collect_workload_correlation(
        self,
        trigger: TriggerContext,
        related_objects: list[dict[str, Any]],
        root_candidates: list[dict[str, Any]],
        evidence_timeline: list[dict[str, Any]],
    ) -> None:
        events = self.client.get_related_events(
            namespace=trigger.workload.namespace,
            kind=trigger.workload.kind,
            name=trigger.workload.name,
        )
        self._append_timeline(evidence_timeline, events.get("items", []), trigger.workload)

        pods = self.client.list_related_pods(
            namespace=trigger.workload.namespace,
            kind=trigger.workload.kind,
            name=trigger.workload.name,
        )
        for item in pods.get("items", []) if isinstance(pods, dict) else []:
            metadata = item.get("metadata", {})
            self._append_related_object(
                related_objects,
                "Pod",
                trigger.workload.namespace,
                metadata.get("name", ""),
                "affected",
            )

        if trigger.workload.kind.lower() == "deployment":
            status = self.client.get_deployment_status(
                namespace=trigger.workload.namespace,
                name=trigger.workload.name,
            )
            conditions = status.get("conditions", []) if isinstance(status, dict) else []
            condition = next(
                (
                    item.get("reason") or item.get("type")
                    for item in conditions
                    if item.get("reason") or item.get("type")
                ),
                "",
            )
            if condition:
                self._append_root_candidate(
                    root_candidates,
                    "Deployment",
                    trigger.workload.namespace,
                    trigger.workload.name,
                    f"Deployment condition {condition} indicates rollout is blocked.",
                    0.82,
                )

    def _append_timeline(
        self,
        timeline: list[dict[str, Any]],
        events: list[dict[str, Any]],
        workload: WorkloadRef,
    ) -> None:
        for item in events[:10]:
            signal = item.get("reason") or item.get("message") or item.get("type") or ""
            timestamp = item.get("timestamp") or ""
            if not signal:
                continue
            timeline.append(
                {
                    "time": timestamp,
                    "objectRef": {
                        "kind": workload.kind,
                        "namespace": workload.namespace,
                        "name": workload.name,
                    },
                    "signal": signal,
                }
            )

    def _append_related_object(
        self,
        items: list[dict[str, Any]],
        kind: str,
        namespace: str,
        name: str,
        role: str,
    ) -> None:
        if not kind or not name:
            return
        candidate = {
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "role": role,
        }
        if candidate not in items:
            items.append(candidate)

    def _append_root_candidate(
        self,
        items: list[dict[str, Any]],
        kind: str,
        namespace: str,
        name: str,
        reason: str,
        confidence: float,
    ) -> None:
        if not kind or not name or not reason:
            return
        candidate = {
            "objectRef": {
                "kind": kind,
                "namespace": namespace,
                "name": name,
            },
            "reason": reason,
            "confidence": confidence,
        }
        if candidate not in items:
            items.append(candidate)

    def _prioritize_root_candidates(
        self,
        symptom: str,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return score_root_cause_candidates(symptom=symptom, items=items)


def build_runtime_service(settings: Settings) -> AgentService:
    from agent.k8s_client.runtime import RuntimeKubernetesClient

    client = RuntimeKubernetesClient(report_namespace=settings.report_namespace)
    responses_client = build_model_client(settings)
    engine = RuleEngine(
        cluster_name=settings.cluster_name,
        min_observation_seconds=settings.min_observation_seconds,
    )
    codex_agent = DiagnosisAgent(
        responses_client=responses_client,
        rule_engine=engine,
        model=responses_client.model,
        max_tool_calls=settings.max_tool_calls,
        max_input_bytes=settings.max_input_bytes,
        max_diagnosis_seconds=settings.max_diagnosis_seconds,
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
    return service


def build_model_client(settings: Settings) -> ModelClient:
    provider = settings.model_provider.lower()
    if provider == "openai":
        return OpenAIResponsesClient(
            api_key=settings.openai_api_key or "",
            model=settings.openai_model,
            api_base_url=settings.api_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
    if provider == "ollama":
        if not settings.ollama_model:
            raise ValueError("OLLAMA_MODEL must be set when MODEL_PROVIDER=ollama")
        return OllamaResponsesClient(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
    raise ValueError(f"Unsupported MODEL_PROVIDER: {settings.model_provider}")
