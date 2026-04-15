from __future__ import annotations

import os
from typing import Any

try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


class FallbackRuleLoader:
    """Loads fallback diagnosis rules from YAML configuration.

    Provides symptom-based fallback rules with severity, probable_causes,
    recommendations, and confidence. Used by RuleEngine when the model
    output is unavailable.
    """

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "fallback_rules.yaml")
        self._rules = self._load(config_path)

    def _load(self, config_path: str) -> dict[str, dict[str, Any]]:
        if not _YAML_AVAILABLE or not os.path.exists(config_path):
            return {}
        try:
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def get_rule(self, symptom: str) -> dict[str, Any] | None:
        """Get fallback rule for a symptom, or None if not found."""
        return self._rules.get(symptom)

    def get_base_fallback(self, symptom: str) -> dict[str, Any]:
        """Get base fallback data for a symptom (used by Python sub-classifiers)."""
        rule = self.get_rule(symptom)
        if rule:
            return {
                "severity": rule.get("severity", "warning"),
                "confidence": rule.get("confidence", 0.35),
                "probable_causes": list(rule.get("probable_causes", [])),
                "recommendations": list(rule.get("recommendations", [])),
            }
        return {
            "severity": "warning",
            "confidence": 0.35,
            "probable_causes": [f"A {symptom} condition is affecting the workload"],
            "recommendations": ["Inspect workload events and cluster conditions"],
        }
