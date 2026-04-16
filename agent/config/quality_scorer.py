from __future__ import annotations

import os
from typing import Any

try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


# Hardcoded defaults matching the original behavior.
# Used when YAML config is unavailable or invalid.
DEFAULT_WEIGHTS = {
    "evidence_coverage": 0.28,
    "recommendation_actionability": 0.20,
    "root_cause_strength": 0.20,
    "correlation_strength": 0.12,
    "confidence_alignment": 0.20,
}
DEFAULT_EVIDENCE_SCALE_MAX = 3.0
DEFAULT_RECOMMENDATION_SCALE_MAX = 2.0
DEFAULT_ROOT_CANDIDATE_SCALE_MAX = 2.0
DEFAULT_CORRELATION_SCALE_MAX = 6.0
DEFAULT_FALLBACK_PENALTY = 0.15
DEFAULT_MIN_SCORE = 0.0
DEFAULT_MAX_SCORE = 1.0


def _clamp(value: float, min_val: float, max_val: float) -> float:
    if value < min_val:
        return min_val
    if value > max_val:
        return max_val
    return float(value)


class QualityScorer:
    """Compute diagnosis quality scores from configuration.

    Reads weights and scale parameters from YAML if available,
    otherwise falls back to hardcoded defaults.
    """

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "quality_scoring.yaml")
        self._config = self._load_config(config_path)

    def _load_config(self, config_path: str) -> dict[str, Any]:
        if not _YAML_AVAILABLE or not os.path.exists(config_path):
            return self._default_config()

        try:
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            return self._default_config()

        return self._normalize(raw)

    def _default_config(self) -> dict[str, Any]:
        return {
            "weights": dict(DEFAULT_WEIGHTS),
            "evidence_scale_max": DEFAULT_EVIDENCE_SCALE_MAX,
            "recommendation_scale_max": DEFAULT_RECOMMENDATION_SCALE_MAX,
            "root_candidate_scale_max": DEFAULT_ROOT_CANDIDATE_SCALE_MAX,
            "correlation_scale_max": DEFAULT_CORRELATION_SCALE_MAX,
            "fallback_penalty": DEFAULT_FALLBACK_PENALTY,
            "min_score": DEFAULT_MIN_SCORE,
            "max_score": DEFAULT_MAX_SCORE,
        }

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        weights = raw.get("weights", {})
        return {
            "weights": {
                "evidence_coverage": float(weights.get("evidence_coverage", DEFAULT_WEIGHTS["evidence_coverage"])),
                "recommendation_actionability": float(
                    weights.get("recommendation_actionability", DEFAULT_WEIGHTS["recommendation_actionability"])
                ),
                "root_cause_strength": float(weights.get("root_cause_strength", DEFAULT_WEIGHTS["root_cause_strength"])),
                "correlation_strength": float(weights.get("correlation_strength", DEFAULT_WEIGHTS["correlation_strength"])),
                "confidence_alignment": float(
                    weights.get("confidence_alignment", DEFAULT_WEIGHTS["confidence_alignment"])
                ),
            },
            "evidence_scale_max": float(raw.get("evidence_scale", {}).get("max_items", DEFAULT_EVIDENCE_SCALE_MAX)),
            "recommendation_scale_max": float(
                raw.get("recommendation_scale", {}).get("max_items", DEFAULT_RECOMMENDATION_SCALE_MAX)
            ),
            "root_candidate_scale_max": float(
                raw.get("root_candidate_scale", {}).get("max_items", DEFAULT_ROOT_CANDIDATE_SCALE_MAX)
            ),
            "correlation_scale_max": float(
                raw.get("correlation_scale", {}).get("max_items", DEFAULT_CORRELATION_SCALE_MAX)
            ),
            "fallback_penalty": float(raw.get("penalties", {}).get("fallback", DEFAULT_FALLBACK_PENALTY)),
            "min_score": float(raw.get("score_bounds", {}).get("min", DEFAULT_MIN_SCORE)),
            "max_score": float(raw.get("score_bounds", {}).get("max", DEFAULT_MAX_SCORE)),
        }

    def compute(
        self,
        *,
        evidence: list[str],
        recommendations: list[str],
        root_candidates: list[dict[str, Any]],
        related_objects: list[dict[str, Any]],
        confidence: float,
        used_fallback: bool,
    ) -> dict[str, Any]:
        c = self._config
        w = c["weights"]
        min_s = c["min_score"]
        max_s = c["max_score"]

        def clamp(value: float) -> float:
            return _clamp(value, min_s, max_s)

        evidence_coverage = clamp(len(evidence) / c["evidence_scale_max"])
        recommendation_actionability = clamp(len(recommendations) / c["recommendation_scale_max"])
        root_cause_strength = clamp(len(root_candidates) / c["root_candidate_scale_max"])
        correlation_strength = clamp((len(related_objects) + len(root_candidates)) / c["correlation_scale_max"])
        confidence_score = clamp(confidence)
        fallback_penalty = c["fallback_penalty"] if used_fallback else 0.0

        weighted = (
            evidence_coverage * w["evidence_coverage"]
            + recommendation_actionability * w["recommendation_actionability"]
            + root_cause_strength * w["root_cause_strength"]
            + correlation_strength * w["correlation_strength"]
            + confidence_score * w["confidence_alignment"]
            - fallback_penalty
        )
        overall = clamp(weighted)

        return {
            "overall": round(overall, 4),
            "method": "rule-v1-yaml",
            "usedFallback": used_fallback,
            "dimensions": {
                "evidenceCoverage": round(evidence_coverage, 4),
                "recommendationActionability": round(recommendation_actionability, 4),
                "rootCauseStrength": round(root_cause_strength, 4),
                "correlationStrength": round(correlation_strength, 4),
                "confidenceAlignment": round(confidence_score, 4),
            },
        }
