from __future__ import annotations

from typing import Any


_PREFERRED_KIND_ORDER: dict[str, list[str]] = {
    "FailedMount": ["PVC", "Pod", "Node"],
    "ProgressDeadlineExceeded": ["Deployment", "ReplicaSet", "Pod"],
    "ReplicaMismatch": ["Deployment", "ReplicaSet", "Pod"],
    "ImagePullBackOff": ["Deployment", "ReplicaSet", "Pod", "Node"],
    "ErrImagePull": ["Deployment", "ReplicaSet", "Pod", "Node"],
    "CreateContainerConfigError": ["Deployment", "ReplicaSet", "Pod", "Node"],
    "ContainerCannotRun": ["Deployment", "ReplicaSet", "Pod", "Node"],
}


def score_root_cause_candidates(
    symptom: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    preferred = _PREFERRED_KIND_ORDER.get(symptom, [])
    total_preferred = max(len(preferred), 1)

    scored: list[dict[str, Any]] = []
    for candidate in items:
        object_ref = candidate.get("objectRef", {}) if isinstance(candidate, dict) else {}
        kind = str(object_ref.get("kind", ""))
        namespace = str(object_ref.get("namespace", ""))
        name = str(object_ref.get("name", ""))
        reason = str(candidate.get("reason", ""))
        confidence = _safe_float(candidate.get("confidence", 0.0))

        kind_score = _kind_score(kind, preferred, total_preferred)
        reason_score = _reason_score(reason)
        final_score = round(_clamp(confidence) * 0.65 + kind_score * 0.25 + reason_score * 0.10, 4)

        rank_reasons: list[str] = []
        if kind_score > 0:
            rank_reasons.append("symptom-kind-priority")
        if reason_score > 0:
            rank_reasons.append("strong-reason-signal")
        rank_reasons.append("base-confidence")

        enriched = dict(candidate)
        enriched["score"] = final_score
        enriched["rankReasons"] = rank_reasons
        scored.append(enriched)

    scored.sort(
        key=lambda item: (
            -_safe_float(item.get("score", 0.0)),
            str(item.get("objectRef", {}).get("kind", "")),
            str(item.get("objectRef", {}).get("namespace", "")),
            str(item.get("objectRef", {}).get("name", "")),
            str(item.get("reason", "")),
        )
    )
    return scored


def _kind_score(kind: str, preferred: list[str], total_preferred: int) -> float:
    if not preferred:
        return 0.0
    try:
        position = preferred.index(kind)
    except ValueError:
        return 0.0
    return (total_preferred - position) / total_preferred


def _reason_score(reason: str) -> float:
    lowered = reason.lower()
    if any(keyword in lowered for keyword in ("multiple pods", "affects multiple pods", "fan-out")):
        return 1.0
    if any(keyword in lowered for keyword in ("blocked", "failed", "not bound", "not found", "deadline")):
        return 0.6
    return 0.0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float) -> float:
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value
