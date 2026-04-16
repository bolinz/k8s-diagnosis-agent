from __future__ import annotations

from typing import Any

from agent.models import DiagnosisResult, TriggerContext
from agent.reporting.diagnosis_reporter import (
    DiagnosisReportFormatter,
    KubernetesDiagnosisReportWriter,
)


class ReportWriter:
    """Orchestrates report persistence.

    Delegates formatting to DiagnosisReportFormatter and
    writing to KubernetesDiagnosisReportWriter.
    """

    def __init__(
        self,
        report_namespace: str,
        formatter: DiagnosisReportFormatter | None = None,
        writer: KubernetesDiagnosisReportWriter | None = None,
        cluster_name: str = "",
    ):
        self._formatter = formatter or DiagnosisReportFormatter()
        self._writer = writer
        self._report_namespace = report_namespace
        self._cluster_name = cluster_name

    def write(
        self,
        trigger: TriggerContext,
        diagnosis: DiagnosisResult,
        model: str,
        prefix: str,
    ) -> dict[str, Any]:
        """Persist a diagnosis report.

        If writer is configured (K8s writer), upserts to cluster.
        Otherwise returns the in-memory report spec/status dict.
        """
        category = self._category_for_symptom(trigger.symptom)
        primary_signal = self._derive_primary_signal(trigger.symptom, trigger.raw_signal)

        if self._writer is None:
            return {
                **self._formatter.build_spec(
                    self._formatter.dedupe_name(trigger, prefix),
                    self._report_namespace,
                    trigger,
                ),
                "status": self._formatter.build_status(
                    diagnosis,
                    model,
                    raw_signal=trigger.raw_signal,
                    category=category,
                    primary_signal=primary_signal,
                ),
            }

        return self._writer.upsert_report(
            trigger,
            diagnosis,
            model,
            prefix,
            category=category,
            primary_signal=primary_signal,
        )

    def list_reports(self, client: Any, params: dict | None = None) -> list[dict]:
        """List reports with optional filters."""
        params = params or {}
        namespace_filter = (params.get("namespace") or [""])[0]
        severity_filter = (params.get("severity") or [""])[0]
        symptom_filter = (params.get("symptom") or [""])[0]
        category_filter = (params.get("category") or [""])[0]
        items = [self._normalize_report(item) for item in client.list_reports()]
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

    def get_report(self, client: Any, name: str) -> dict | None:
        """Get a single report by name."""
        report = client.get_report(name)
        if report is None:
            return None
        return self._normalize_report(report)

    def backfill(
        self,
        client: Any,
        enricher: Any,
        codex_agent: Any,
        active_model_name: str,
        prefix: str,
    ) -> list[dict]:
        """Backfill incomplete reports with fallback diagnoses."""
        updated = []
        if self._writer is None:
            return updated
        for report in client.list_reports():
            status = report.get("status", {})
            if (
                self._has_text_value(status.get("summary"))
                and self._has_text_items(status.get("evidence"))
                and self._has_text_items(status.get("recommendations"))
            ):
                continue
            trigger = self._trigger_from_report(report)
            diagnosis = enricher.ensure_complete(
                trigger,
                codex_agent.rule_engine.fallback_diagnosis(trigger),
                correlation={},
            )
            updated.append(
                self._writer.upsert_report(
                    trigger,
                    diagnosis,
                    active_model_name,
                    prefix,
                )
            )
        return updated

    def _normalize_report(self, report: dict) -> dict:
        """Normalize report shape for API responses."""
        metadata = report.get("metadata", {})
        spec = report.get("spec", {})
        status = report.get("status", {})
        spec = self._normalize_report_spec(spec, status.get("rawSignal", {}))
        status = self._normalize_report_status(status)
        trigger = self._normalize_trigger_context(spec, status)
        # Sanitize rootCauseCandidates using the sanitized relatedObjects and trigger
        status["rootCauseCandidates"] = self._sanitize_root_cause_candidates_report(
            status.get("rootCauseCandidates", []),
            status.get("relatedObjects", []),
            trigger,
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
            "category": self._normalize_category(status.get("category", ""), spec.get("symptom", "")),
            "primarySignal": self._normalize_primary_signal(
                status.get("primarySignal", ""),
                status.get("rawSignal", {}),
                spec.get("symptom", ""),
            ),
        }

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
        normalized["cluster"] = normalized.get("cluster") or self._cluster_name
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
        # rootCauseCandidates sanitization happens in _normalize_report after we have the trigger
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

    def _normalize_trigger_context(self, spec: dict, status: dict) -> TriggerContext:
        from agent.models import TriggerContext, WorkloadRef
        return TriggerContext(
            source=spec.get("source", "scheduled"),
            cluster=spec.get("cluster", ""),
            workload=WorkloadRef(
                kind=spec.get("workloadRef", {}).get("kind", "Pod"),
                namespace=spec.get("namespace", "default"),
                name=spec.get("workloadRef", {}).get("name", ""),
            ),
            symptom=spec.get("symptom", ""),
            observed_for_seconds=int(spec.get("observedFor", 0) or 0),
            raw_signal=status.get("rawSignal", {}),
        )

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

    def _fallback_summary(self, trigger: TriggerContext) -> str:
        workload_name = trigger.workload.name or "workload"
        return (
            f"Detected {trigger.symptom} for "
            f"{trigger.workload.kind}/{workload_name} in {trigger.workload.namespace}."
        )

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

    def _trigger_from_report(self, report: dict) -> TriggerContext:
        from agent.models import TriggerContext, WorkloadRef
        spec = report.get("spec", {})
        workload = spec.get("workloadRef", {})
        status = report.get("status", {})
        return TriggerContext(
            source=spec.get("source", "scheduled"),
            cluster=spec.get("cluster", ""),
            workload=WorkloadRef(
                kind=workload.get("kind", "Pod"),
                namespace=spec.get("namespace", "default"),
                name=workload.get("name", ""),
            ),
            symptom=spec.get("symptom", "Pending"),
            observed_for_seconds=int(spec.get("observedFor", 0)),
            raw_signal=status.get("rawSignal", {}),
        )

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

    def _sanitize_root_cause_candidates_report(
        self,
        items: list[dict[str, Any]],
        related_objects: list[dict[str, Any]],
        trigger: TriggerContext,
    ) -> list[dict[str, Any]]:
        """Sanitize root cause candidates for report listing.

        Filters out candidates where:
        - kind or name is a placeholder value
        - objectRef is not in relatedObjects AND not the primary workload
        """
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
            normalized = {
                "objectRef": {"kind": kind, "namespace": namespace, "name": name},
                "reason": reason,
                "confidence": float(item.get("confidence", 0.6)) if item.get("confidence") is not None else 0.6,
            }
            if normalized not in sanitized:
                sanitized.append(normalized)
        return sanitized

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

    def _is_placeholder_value(self, value: Any) -> bool:
        text = str(value or "").strip().lower()
        return text in {"unknown", "n/a", "na", "none", "null", "<unknown>"}
