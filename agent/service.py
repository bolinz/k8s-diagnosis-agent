from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
import logging
import re
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
        workload_ref = payload.get("workloadRef", {})
        workload = workload_ref if isinstance(workload_ref, dict) else {}
        namespace = (
            str(payload.get("namespace", "")).strip()
            or str(workload.get("namespace", "")).strip()
            or "default"
        )
        name = (
            str(payload.get("name", "")).strip()
            or str(payload.get("pod_name", "")).strip()
            or str(workload.get("name", "")).strip()
        )
        kind = (
            str(payload.get("kind", "")).strip()
            or str(workload.get("kind", "")).strip()
            or "Pod"
        )
        symptom = str(payload.get("symptom", "Pending")).strip() or "Pending"
        observed_for_seconds = self._coerce_observed_seconds(
            payload.get("observed_for_seconds", payload.get("observedFor", 0))
        )
        trigger_at = self._parse_trigger_time(payload)
        cluster = str(payload.get("cluster", "")).strip() or self.settings.cluster_name
        log_event(
            LOGGER,
            logging.INFO,
            "alert_received",
            "alert webhook received",
            namespace=namespace,
            workload_name=name or "",
            workload_kind=kind,
            symptom=symptom,
        )
        trigger = TriggerContext(
            source="alert",
            cluster=cluster,
            workload=WorkloadRef(kind=kind, namespace=namespace, name=name),
            symptom=symptom,
            observed_for_seconds=observed_for_seconds,
            trigger_at=trigger_at,
            raw_signal=payload,
        )
        return self.process_trigger(trigger)

    def _coerce_observed_seconds(self, value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                return 0
            if normalized.endswith("s"):
                normalized = normalized[:-1].strip()
            try:
                return max(0, int(float(normalized)))
            except ValueError:
                return 0
        return 0

    def _parse_trigger_time(self, payload: dict[str, Any]) -> datetime:
        raw_value = payload.get("triggerAt") or payload.get("trigger_at")
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if value:
                normalized = value.replace("Z", "+00:00")
                try:
                    parsed = datetime.fromisoformat(normalized)
                    if parsed.tzinfo is None:
                        return parsed.replace(tzinfo=timezone.utc)
                    return parsed
                except ValueError:
                    pass
        return datetime.now(timezone.utc)

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
            self.codex_agent.fallback_diagnosis(
                aggregated_trigger,
                reason="event_storm_aggregated",
                storm_count=int(state.get("count", 0)),
                storm_window_seconds=window_seconds,
            ),
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
            if self._has_text_value(status.get("summary")) and self._has_text_items(status.get("evidence")) and self._has_text_items(
                status.get("recommendations")
            ):
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
        trigger = self._normalize_trigger(
            TriggerContext(
                source=spec.get("source", "scheduled"),
                cluster=spec.get("cluster", self.settings.cluster_name),
                workload=WorkloadRef(
                    kind=spec.get("workloadRef", {}).get("kind", "Pod"),
                    namespace=spec.get("namespace", "default"),
                    name=spec.get("workloadRef", {}).get("name", ""),
                ),
                symptom=spec.get("symptom", ""),
                observed_for_seconds=int(spec.get("observedFor", 0) or 0),
                raw_signal=status.get("rawSignal", {}),
            )
        )
        summary = str(status.get("summary", "")).strip()
        if not summary:
            summary = self._fallback_summary(trigger)
        probable_causes = self._clean_text_list(status.get("probableCauses"))
        if not probable_causes:
            probable_causes = self._fallback_probable_causes(trigger)
        evidence = self._clean_text_list(status.get("evidence"))
        if not evidence:
            evidence = self._fallback_evidence(trigger)
        recommendations = self._clean_text_list(status.get("recommendations"))
        if not recommendations:
            recommendations = self._fallback_recommendations(trigger, [])
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
            "summary": summary,
            "probableCauses": probable_causes,
            "evidence": evidence,
            "recommendations": recommendations,
            "confidence": status.get("confidence", 0.0),
            "relatedObjects": status.get("relatedObjects", []),
            "rootCauseCandidates": status.get("rootCauseCandidates", []),
            "evidenceTimeline": status.get("evidenceTimeline", []),
            "impactSummary": status.get("impactSummary", {}),
            "qualityScore": status.get("qualityScore", {}),
            "uncertainties": status.get("uncertainties", []),
            "evidenceAttribution": status.get("evidenceAttribution", []),
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
        related_objects = self._sanitize_related_objects(self._merge_related_objects(
            correlation.get("relatedObjects", []),
            diagnosis.related_objects,
        ))
        root_candidates = self._sanitize_root_cause_candidates(
            self._merge_root_cause_candidates(
            correlation.get("rootCauseCandidates", []),
            diagnosis.root_cause_candidates,
            ),
            related_objects,
            trigger,
        )
        probable_causes = self._clean_text_list(diagnosis.probable_causes)
        if not probable_causes:
            probable_causes = self._clean_text_list(fallback.probable_causes) or self._fallback_probable_causes(trigger)
        if not root_candidates:
            root_candidates = self._minimal_root_cause_candidates(
                trigger,
                related_objects,
                probable_causes,
            )
        recommendations = self._clean_text_list(diagnosis.recommendations) or self._fallback_recommendations(
            trigger,
            self._clean_text_list(fallback.recommendations),
        )
        evidence = self._clean_text_list(diagnosis.evidence) or self._fallback_evidence(trigger)
        used_fallback = (
            diagnosis.used_fallback
            or not self._has_text_value(diagnosis.summary)
            or not self._has_text_items(diagnosis.evidence)
            or not self._has_text_items(diagnosis.recommendations)
        )
        uncertainties = self._build_uncertainties(
            trigger=trigger,
            used_fallback=used_fallback,
            evidence=evidence,
            recommendations=recommendations,
            root_candidates=root_candidates,
        )
        quality_score = self._compute_quality_score(
            evidence=evidence,
            recommendations=recommendations,
            root_candidates=root_candidates,
            related_objects=related_objects,
            confidence=diagnosis.confidence if diagnosis.confidence > 0 else fallback.confidence,
            used_fallback=used_fallback,
        )
        evidence_timeline = diagnosis.evidence_timeline or correlation.get("evidenceTimeline", [])
        evidence_attribution = self._build_evidence_attribution(
            trigger=trigger,
            diagnosis=diagnosis,
            evidence_timeline=[item for item in evidence_timeline if isinstance(item, dict)],
        )
        return DiagnosisResult(
            summary=summary,
            severity=diagnosis.severity or fallback.severity,
            probable_causes=probable_causes,
            evidence=evidence,
            recommendations=recommendations,
            confidence=diagnosis.confidence if diagnosis.confidence > 0 else fallback.confidence,
            related_objects=related_objects,
            root_cause_candidates=root_candidates,
            evidence_timeline=evidence_timeline,
            impact_summary=diagnosis.impact_summary or correlation.get("impactSummary", {}),
            quality_score=quality_score,
            uncertainties=uncertainties,
            evidence_attribution=evidence_attribution,
            raw_agent_output=diagnosis.raw_agent_output,
            used_fallback=used_fallback,
        )

    def _merge_related_objects(
        self,
        correlation_items: list[dict[str, Any]],
        diagnosis_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for item in list(correlation_items or []) + list(diagnosis_items or []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip()
            name = str(item.get("name", "")).strip()
            if not kind or not name:
                continue
            normalized = {
                "kind": kind,
                "namespace": str(item.get("namespace", "")).strip(),
                "name": name,
                "role": str(item.get("role", "")).strip() or "affected",
            }
            if normalized not in merged:
                merged.append(normalized)
        return merged

    def _sanitize_related_objects(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip()
            name = str(item.get("name", "")).strip()
            if not kind or not name:
                continue
            if self._is_placeholder_value(kind) or self._is_placeholder_value(name):
                continue
            normalized = {
                "kind": kind,
                "namespace": str(item.get("namespace", "")).strip(),
                "name": name,
                "role": str(item.get("role", "")).strip() or "affected",
            }
            if normalized not in sanitized:
                sanitized.append(normalized)
        return sanitized

    def _merge_root_cause_candidates(
        self,
        correlation_items: list[dict[str, Any]],
        diagnosis_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for item in list(correlation_items or []) + list(diagnosis_items or []):
            if not isinstance(item, dict):
                continue
            ref = item.get("objectRef", {})
            if not isinstance(ref, dict):
                continue
            kind = str(ref.get("kind", "")).strip()
            name = str(ref.get("name", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if not kind or not name or not reason:
                continue
            confidence = item.get("confidence", 0.6)
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                confidence_value = 0.6
            normalized = {
                "objectRef": {
                    "kind": kind,
                    "namespace": str(ref.get("namespace", "")).strip(),
                    "name": name,
                },
                "reason": reason,
                "confidence": confidence_value,
            }
            if normalized not in merged:
                merged.append(normalized)
        return merged

    def _sanitize_root_cause_candidates(
        self,
        items: list[dict[str, Any]],
        related_objects: list[dict[str, Any]],
        trigger: TriggerContext,
    ) -> list[dict[str, Any]]:
        allowed_refs = {
            (
                str(item.get("kind", "")).strip().lower(),
                str(item.get("namespace", "")).strip().lower(),
                str(item.get("name", "")).strip().lower(),
            )
            for item in related_objects
            if isinstance(item, dict)
        }
        primary_ref = (
            trigger.workload.kind.strip().lower(),
            trigger.workload.namespace.strip().lower(),
            trigger.workload.name.strip().lower(),
        )
        sanitized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = item.get("objectRef", {})
            if not isinstance(ref, dict):
                continue
            kind = str(ref.get("kind", "")).strip()
            namespace = str(ref.get("namespace", "")).strip()
            name = str(ref.get("name", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if not kind or not name or not reason:
                continue
            if self._is_placeholder_value(kind) or self._is_placeholder_value(name):
                continue
            key = (kind.lower(), namespace.lower(), name.lower())
            if key not in allowed_refs and key != primary_ref:
                continue
            try:
                confidence_value = float(item.get("confidence", 0.6))
            except (TypeError, ValueError):
                confidence_value = 0.6
            normalized = {
                "objectRef": {"kind": kind, "namespace": namespace, "name": name},
                "reason": reason,
                "confidence": confidence_value,
            }
            if normalized not in sanitized:
                sanitized.append(normalized)
        return sanitized

    def _minimal_root_cause_candidates(
        self,
        trigger: TriggerContext,
        related_objects: list[dict[str, Any]],
        probable_causes: list[str],
    ) -> list[dict[str, Any]]:
        likely_pvc = next(
            (
                item
                for item in related_objects
                if item.get("kind") == "PVC" and item.get("name")
            ),
            None,
        )
        if likely_pvc and trigger.symptom in {"Pending", "FailedMount"}:
            pvc_name = likely_pvc.get("name", "")
            return [
                {
                    "objectRef": {
                        "kind": "PVC",
                        "namespace": likely_pvc.get("namespace", trigger.workload.namespace),
                        "name": pvc_name,
                    },
                    "reason": (
                        f"PVC {pvc_name} appears related to scheduling/mount failure "
                        f"for {trigger.workload.kind}/{trigger.workload.name}."
                    ),
                    "confidence": 0.72,
                }
            ]
        upstream = next(
            (
                item
                for item in related_objects
                if item.get("role") in {"owner", "upstream-suspect"}
                and item.get("kind")
                and item.get("name")
            ),
            None,
        )
        if upstream:
            cause_hint = probable_causes[0] if probable_causes else "upstream dependency appears unhealthy"
            return [
                {
                    "objectRef": {
                        "kind": upstream.get("kind", ""),
                        "namespace": upstream.get("namespace", ""),
                        "name": upstream.get("name", ""),
                    },
                    "reason": f"{cause_hint}. Related object {upstream.get('kind')}/{upstream.get('name')} is likely involved.",
                    "confidence": 0.58,
                }
            ]
        cause_hint = probable_causes[0] if probable_causes else "insufficient evidence from model output"
        return [
            {
                "objectRef": {
                    "kind": trigger.workload.kind,
                    "namespace": trigger.workload.namespace,
                    "name": trigger.workload.name,
                },
                "reason": f"{cause_hint}. Primary workload remains the best candidate.",
                "confidence": 0.45,
            }
        ]

    def _normalize_report_spec(self, spec: dict[str, Any], raw_signal: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(spec)
        workload = dict(normalized.get("workloadRef", {}))
        involved = raw_signal.get("involvedObject", {}) if isinstance(raw_signal, dict) else {}
        signal_workload = raw_signal.get("workloadRef", {}) if isinstance(raw_signal, dict) else {}
        kind = (
            workload.get("kind")
            or involved.get("kind")
            or (signal_workload.get("kind") if isinstance(signal_workload, dict) else "")
            or "Pod"
        )
        name = (
            workload.get("name")
            or involved.get("name")
            or (signal_workload.get("name") if isinstance(signal_workload, dict) else "")
            or ""
        )
        namespace = (
            normalized.get("namespace")
            or involved.get("namespace")
            or (signal_workload.get("namespace") if isinstance(signal_workload, dict) else "")
            or "default"
        )
        kind = "" if self._is_placeholder_value(kind) else str(kind).strip() or "Pod"
        name = "" if self._is_placeholder_value(name) else str(name).strip()
        namespace = "default" if self._is_placeholder_value(namespace) else str(namespace).strip() or "default"
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
        normalized_model_info: dict[str, Any] = {}
        if isinstance(model_info, dict):
            name = model_info.get("name") or model_info.get("model")
            provider = model_info.get("provider")
            if name and not self._is_placeholder_value(name):
                normalized_model_info["name"] = str(name).strip()
            if provider and not self._is_placeholder_value(provider):
                normalized_model_info["provider"] = str(provider).strip()
            if "fallback" in model_info:
                normalized_model_info["fallback"] = bool(model_info.get("fallback"))
            if model_info.get("traceId"):
                normalized_model_info["traceId"] = str(model_info.get("traceId"))
        normalized["modelInfo"] = normalized_model_info
        raw_signal = normalized.get("rawSignal")
        normalized["rawSignal"] = raw_signal if isinstance(raw_signal, dict) else {}
        trace = normalized.get("diagnosisTrace")
        normalized["diagnosisTrace"] = trace if isinstance(trace, dict) else {}
        normalized["relatedObjects"] = self._sanitize_related_objects(
            [item for item in normalized.get("relatedObjects", []) if isinstance(item, dict)]
        )
        report_workload = raw_signal if isinstance(raw_signal, dict) else {}
        normalized["rootCauseCandidates"] = self._sanitize_root_cause_candidates(
            [item for item in normalized.get("rootCauseCandidates", []) if isinstance(item, dict)],
            normalized["relatedObjects"],
            TriggerContext(
                source="scheduled",
                cluster=self.settings.cluster_name,
                workload=WorkloadRef(
                    kind=str(report_workload.get("kind", "Pod") or "Pod"),
                    namespace=str(report_workload.get("namespace", "")),
                    name=str(report_workload.get("name", "")),
                ),
                symptom=str(report_workload.get("symptom", "")),
                observed_for_seconds=0,
            ),
        )
        normalized["evidenceTimeline"] = [
            item
            for item in normalized.get("evidenceTimeline", [])
            if isinstance(item, dict)
        ]
        quality_score = normalized.get("qualityScore")
        normalized["qualityScore"] = quality_score if isinstance(quality_score, dict) else {}
        normalized["uncertainties"] = self._clean_text_list(normalized.get("uncertainties"))
        normalized["evidenceAttribution"] = [
            item
            for item in normalized.get("evidenceAttribution", [])
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
        workload_name = trigger.workload.name or "workload"
        summary = (
            f"Detected {trigger.symptom} for "
            f"{trigger.workload.kind}/{workload_name} in {trigger.workload.namespace}."
        )
        reason = trigger.raw_signal.get("reason")
        if trigger.source == "event" and reason:
            summary += f" Triggered by Kubernetes event {reason}."
        return summary

    def _fallback_probable_causes(self, trigger: TriggerContext) -> list[str]:
        message = str(trigger.raw_signal.get("message", "")).lower()
        symptom = trigger.symptom
        if symptom == "FailedMount":
            if "secret" in message and "not found" in message:
                return ["A required Secret for volume mount is missing in the namespace"]
            if "configmap" in message and "not found" in message:
                return ["A required ConfigMap for volume mount is missing in the namespace"]
            if "persistentvolumeclaim" in message or "pvc" in message:
                return ["A referenced PVC is not bound or unavailable for mounting"]
        if symptom == "Pending":
            if "unbound immediate persistentvolumeclaims" in message:
                return ["One or more referenced PVCs are unbound, blocking scheduling"]
            if "insufficient" in message:
                return ["Cluster resources are insufficient for this workload scheduling request"]
            return ["Scheduler constraints or workload dependencies are preventing pod placement"]
        return []

    def _fallback_recommendations(self, trigger: TriggerContext, base: list[str]) -> list[str]:
        recommendations = self._clean_text_list(base)
        message = str(trigger.raw_signal.get("message", "")).lower()
        if trigger.symptom == "Pending":
            hints = ["Review FailedScheduling events for taints, affinity, quota, and resource shortage signals"]
            if "unbound immediate persistentvolumeclaims" in message:
                hints.append("Verify referenced PVCs are Bound and storageClass exists")
            for hint in hints:
                if hint not in recommendations:
                    recommendations.append(hint)
        if trigger.symptom == "FailedMount":
            hints = ["Inspect pod events and verify referenced Secret/ConfigMap/PVC objects exist"]
            if "secret" in message and "not found" in message:
                hints.append("Create the missing Secret or correct the secret reference in volume/env configuration")
            if "configmap" in message and "not found" in message:
                hints.append("Create the missing ConfigMap or correct the configMap reference in volume/env configuration")
            for hint in hints:
                if hint not in recommendations:
                    recommendations.append(hint)
        if not recommendations:
            recommendations.append("Inspect recent workload events and container status to validate the most likely root cause")
        return recommendations

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
        workload_name = "" if self._is_placeholder_value(workload_name) else str(workload_name).strip()
        workload_kind = "Pod" if self._is_placeholder_value(workload_kind) else str(workload_kind).strip() or "Pod"
        workload_namespace = (
            "default"
            if self._is_placeholder_value(workload_namespace)
            else str(workload_namespace).strip() or "default"
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

    def _is_placeholder_value(self, value: Any) -> bool:
        text = str(value or "").strip().lower()
        return text in {"unknown", "n/a", "na", "none", "null", "<unknown>"}

    def _clean_text_list(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        cleaned: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if not text:
                continue
            if self._is_placeholder_value(text):
                continue
            cleaned.append(text)
        return cleaned

    def _has_text_items(self, values: Any) -> bool:
        return bool(self._clean_text_list(values))

    def _has_text_value(self, value: Any) -> bool:
        text = str(value or "").strip()
        return bool(text) and not self._is_placeholder_value(text)

    def _compute_quality_score(
        self,
        *,
        evidence: list[str],
        recommendations: list[str],
        root_candidates: list[dict[str, Any]],
        related_objects: list[dict[str, Any]],
        confidence: float,
        used_fallback: bool,
    ) -> dict[str, Any]:
        evidence_coverage = self._clamp_score(len(evidence) / 3.0)
        recommendation_actionability = self._clamp_score(len(recommendations) / 2.0)
        root_cause_strength = self._clamp_score(len(root_candidates) / 2.0)
        correlation_strength = self._clamp_score((len(related_objects) + len(root_candidates)) / 6.0)
        confidence_score = self._clamp_score(confidence)
        fallback_penalty = 0.15 if used_fallback else 0.0

        weighted = (
            evidence_coverage * 0.28
            + recommendation_actionability * 0.20
            + root_cause_strength * 0.20
            + correlation_strength * 0.12
            + confidence_score * 0.20
            - fallback_penalty
        )
        overall = self._clamp_score(weighted)
        return {
            "overall": round(overall, 4),
            "method": "rule-v1",
            "usedFallback": used_fallback,
            "dimensions": {
                "evidenceCoverage": round(evidence_coverage, 4),
                "recommendationActionability": round(recommendation_actionability, 4),
                "rootCauseStrength": round(root_cause_strength, 4),
                "correlationStrength": round(correlation_strength, 4),
                "confidenceAlignment": round(confidence_score, 4),
            },
        }

    def _build_uncertainties(
        self,
        *,
        trigger: TriggerContext,
        used_fallback: bool,
        evidence: list[str],
        recommendations: list[str],
        root_candidates: list[dict[str, Any]],
    ) -> list[str]:
        items: list[str] = []
        if used_fallback:
            items.append("Diagnosis used fallback logic due to incomplete or constrained model output.")
        if not root_candidates:
            items.append("No high-confidence root cause candidate was identified.")
        if len(evidence) < 2:
            items.append("Evidence coverage is limited; verify with additional workload and event context.")
        if len(recommendations) < 2:
            items.append("Recommendation set is minimal; validate actions against live cluster state.")
        if trigger.source == "event" and not trigger.raw_signal.get("reason"):
            items.append("Event reason is missing from trigger metadata.")
        return items

    def _clamp_score(self, value: float) -> float:
        if value < 0:
            return 0.0
        if value > 1:
            return 1.0
        return float(value)

    def _build_evidence_attribution(
        self,
        *,
        trigger: TriggerContext,
        diagnosis: DiagnosisResult,
        evidence_timeline: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for entry in diagnosis.evidence_attribution:
            if isinstance(entry, dict):
                items.append(dict(entry))

        trace = diagnosis.raw_agent_output.get("trace", {}) if isinstance(diagnosis.raw_agent_output, dict) else {}
        tool_sequence = trace.get("toolSequence", []) if isinstance(trace, dict) else []
        for index, call in enumerate(tool_sequence, start=1):
            if not isinstance(call, dict):
                continue
            tool_name = str(call.get("name", "")).strip()
            if not tool_name:
                continue
            items.append(
                {
                    "source": "tool",
                    "tool": tool_name,
                    "signal": f"Tool {tool_name} executed",
                    "sequence": index,
                    "durationMs": int(call.get("durationMs", 0) or 0),
                    "scopeGuardHit": bool(call.get("scopeGuardHit", False)),
                }
            )

        for point in evidence_timeline[:8]:
            signal = str(point.get("signal", "")).strip()
            if not signal:
                continue
            attribution: dict[str, Any] = {
                "source": "timeline",
                "signal": signal,
            }
            timestamp = str(point.get("time", "")).strip()
            if timestamp:
                attribution["time"] = timestamp
            object_ref = self._normalize_object_ref(point.get("objectRef", {}))
            if object_ref:
                attribution["objectRef"] = object_ref
            items.append(attribution)

        if trigger.source == "event":
            reason = str(trigger.raw_signal.get("reason", "")).strip()
            message = str(trigger.raw_signal.get("message", "")).strip()
            timestamp = str(trigger.raw_signal.get("timestamp", "")).strip()
            signal = reason or message
            if signal:
                event_item: dict[str, Any] = {
                    "source": "trigger",
                    "signal": signal,
                    "objectRef": {
                        "kind": trigger.workload.kind,
                        "namespace": trigger.workload.namespace,
                        "name": trigger.workload.name,
                    },
                }
                if reason:
                    event_item["reason"] = reason
                if message:
                    event_item["message"] = message
                if timestamp:
                    event_item["time"] = timestamp
                items.append(event_item)

        return self._dedupe_evidence_attribution(items)

    def _dedupe_evidence_attribution(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str, str, str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            signal = str(item.get("signal", "")).strip()
            tool = str(item.get("tool", "")).strip()
            timestamp = str(item.get("time", "")).strip()
            object_ref = self._normalize_object_ref(item.get("objectRef", {}))
            key = (
                source,
                tool,
                signal,
                timestamp,
                object_ref.get("kind", ""),
                object_ref.get("name", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            normalized = dict(item)
            if source:
                normalized["source"] = source
            if signal:
                normalized["signal"] = signal
            if tool:
                normalized["tool"] = tool
            if timestamp:
                normalized["time"] = timestamp
            if object_ref:
                normalized["objectRef"] = object_ref
            deduped.append(normalized)
        return deduped

    def _normalize_object_ref(self, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        kind = str(value.get("kind", "")).strip()
        name = str(value.get("name", "")).strip()
        namespace = str(value.get("namespace", "")).strip()
        if not kind or not name:
            return {}
        return {
            "kind": kind,
            "namespace": namespace,
            "name": name,
        }

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
        for event in events.get("items", []) if isinstance(events, dict) else []:
            reason = str(event.get("reason", "")).strip()
            message = str(event.get("message", "")).strip()
            if reason != "FailedScheduling":
                continue
            if "unbound immediate persistentvolumeclaims" not in message.lower():
                continue
            match = re.search(r'["\']([a-z0-9-]+)["\']', message, flags=re.IGNORECASE)
            if not match:
                continue
            pvc_name = match.group(1)
            self._append_root_candidate(
                root_candidates,
                "PVC",
                trigger.workload.namespace,
                pvc_name,
                "Scheduling failed because at least one referenced PVC is unbound.",
                0.68,
            )

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
