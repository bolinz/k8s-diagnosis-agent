from __future__ import annotations

from typing import Any

from agent.analyzers.rules import RuleEngine
from agent.config.quality_scorer import QualityScorer
from agent.models import DiagnosisResult, TriggerContext


class DiagnosisEnricher:
    """Enriches and validates diagnosis results.

    Fills missing fields, computes quality scores, builds evidence attribution,
    and applies fallback logic when the model output is incomplete.
    """

    def __init__(self, rule_engine: RuleEngine):
        self._rule_engine = rule_engine
        self._scorer = QualityScorer()

    def ensure_complete(
        self,
        trigger: TriggerContext,
        diagnosis: DiagnosisResult,
        correlation: dict[str, Any],
    ) -> DiagnosisResult:
        """Ensure diagnosis has all required fields, applying fallback where needed."""
        fallback = self._rule_engine.fallback_diagnosis(trigger)
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
        return self._scorer.compute(
            evidence=evidence,
            recommendations=recommendations,
            root_candidates=root_candidates,
            related_objects=related_objects,
            confidence=confidence,
            used_fallback=used_fallback,
        )

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

        if not items:
            fallback_item: dict[str, Any] = {
                "source": "fallback" if diagnosis.used_fallback else "trigger",
                "signal": trigger.symptom or "diagnosis",
                "objectRef": {
                    "kind": trigger.workload.kind,
                    "namespace": trigger.workload.namespace,
                    "name": trigger.workload.name,
                },
            }
            if diagnosis.used_fallback:
                fallback_item["reason"] = "No tool/timeline evidence available; using minimal trigger attribution."
            items.append(fallback_item)

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
