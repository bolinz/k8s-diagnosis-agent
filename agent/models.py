from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WorkloadRef:
    kind: str
    namespace: str
    name: str


@dataclass
class TriggerContext:
    source: str
    cluster: str
    workload: WorkloadRef
    symptom: str
    observed_for_seconds: int
    trigger_at: datetime = field(default_factory=utc_now)
    raw_signal: dict[str, Any] = field(default_factory=dict)
    correlation_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiagnosisIssue:
    title: str
    detail: str
    remediation: str


@dataclass
class DiagnosisResult:
    summary: str
    severity: str
    probable_causes: list[str]
    evidence: list[str]
    recommendations: list[str]
    confidence: float
    related_objects: list[dict[str, Any]] = field(default_factory=list)
    root_cause_candidates: list[dict[str, Any]] = field(default_factory=list)
    evidence_timeline: list[dict[str, Any]] = field(default_factory=list)
    impact_summary: dict[str, Any] = field(default_factory=dict)
    raw_agent_output: dict[str, Any] = field(default_factory=dict)
    used_fallback: bool = False


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    output: str


@dataclass
class PendingFinding:
    trigger: TriggerContext
    rule_hint: str
