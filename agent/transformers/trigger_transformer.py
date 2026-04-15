from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from agent.analyzers.attribution import score_root_cause_candidates
from agent.models import TriggerContext, WorkloadRef


class TriggerTransformer:
    """Transforms triggers through normalize → augment → correlate pipeline.

    Stateless; K8s client passed at construction for correlation queries.
    """

    def __init__(self, client: Any, cluster_name: str):
        self._client = client
        self._cluster_name = cluster_name

    def transform(self, trigger: TriggerContext) -> tuple[TriggerContext, dict[str, Any]]:
        """Full pipeline: normalize → augment → correlate.

        Returns (transformed_trigger, correlation_context).
        """
        normalized = self.normalize(trigger)
        augmented = self.augment(normalized)
        correlated = self.correlate(augmented)
        return correlated, correlated.correlation_context or {}

    def normalize(self, trigger: TriggerContext) -> TriggerContext:
        """Normalize trigger fields and raw signal."""
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
            cluster=trigger.cluster or self._cluster_name,
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
        raw_signal["timestamp"] = raw_signal.get("timestamp") or (
            trigger.trigger_at.astimezone(timezone.utc).isoformat() if trigger.trigger_at else ""
        )
        return raw_signal

    def augment(self, trigger: TriggerContext) -> TriggerContext:
        """Augment trigger with additional cluster context."""
        raw_signal = dict(trigger.raw_signal)
        try:
            if trigger.workload.kind.lower() == "pod" and trigger.workload.name:
                pod_conditions = self._client.get_pod_conditions(
                    namespace=trigger.workload.namespace,
                    pod_name=trigger.workload.name,
                )
                raw_signal["podPhase"] = raw_signal.get("podPhase") or pod_conditions.get("phase")
                raw_signal["podReason"] = raw_signal.get("podReason") or pod_conditions.get("reason")
                statuses = self._client.get_container_statuses(
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
                    pvc_status = self._client.get_pvc_status(
                        namespace=trigger.workload.namespace,
                        pod_name=trigger.workload.name,
                    )
                    items = pvc_status.get("items", [])
                    if items:
                        raw_signal["pvcPhase"] = raw_signal.get("pvcPhase") or items[0].get("phase")
            elif trigger.workload.kind.lower() == "deployment" and trigger.workload.name:
                deployment_status = self._client.get_deployment_status(
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

    def correlate(self, trigger: TriggerContext) -> TriggerContext:
        """Attach correlation context to trigger."""
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

        reports = self._client.get_related_reports(
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
        owner_chain = self._client.get_owner_chain(
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

        events = self._client.get_related_events(
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

        spec = self._client.get_pod_spec_summary(
            namespace=trigger.workload.namespace,
            pod_name=trigger.workload.name,
        )
        node_name = spec.get("nodeName") if isinstance(spec, dict) else None
        if node_name:
            self._append_related_object(related_objects, "Node", "", node_name, "upstream-suspect")
            impact = self._client.get_node_workload_impact(node_name=node_name)
            if impact.get("podCount", 0) > 1:
                self._append_root_candidate(
                    root_candidates,
                    "Node",
                    "",
                    node_name,
                    f"Node {node_name} currently affects multiple pods on the same host.",
                    0.72,
                )

        pvcs = self._client.get_attached_pvcs(
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
                dependents = self._client.get_pvc_dependents(
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
                pods = self._client.list_related_pods(
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
        events = self._client.get_related_events(
            namespace=trigger.workload.namespace,
            kind=trigger.workload.kind,
            name=trigger.workload.name,
        )
        self._append_timeline(evidence_timeline, events.get("items", []), trigger.workload)

        pods = self._client.list_related_pods(
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
            status = self._client.get_deployment_status(
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

    def _is_placeholder_value(self, value: Any) -> bool:
        text = str(value or "").strip().lower()
        return text in {"unknown", "n/a", "na", "none", "null", "<unknown>"}
