from __future__ import annotations

from threading import Lock
from typing import Any


_LOCK = Lock()
_COUNTERS: dict[str, float] = {
    "k8s_diagnosis_requests_total": 0.0,
    "k8s_diagnosis_fallback_total": 0.0,
    "k8s_diagnosis_tool_calls_total": 0.0,
    "k8s_diagnosis_report_upsert_total": 0.0,
    "k8s_diagnosis_uncertainty_total": 0.0,
    "k8s_diagnosis_batch_report_total": 0.0,
}
_DURATION_SUM = 0.0
_DURATION_COUNT = 0.0
_QUALITY_SCORE_SUM = 0.0
_QUALITY_SCORE_COUNT = 0.0
_BATCH_SIZE_SUM = 0.0
_BATCH_SIZE_COUNT = 0.0


def inc_counter(name: str, amount: float = 1.0) -> None:
    if amount <= 0:
        return
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0.0) + amount


def observe_diagnosis_duration(seconds: float) -> None:
    if seconds < 0:
        return
    with _LOCK:
        global _DURATION_SUM, _DURATION_COUNT
        _DURATION_SUM += seconds
        _DURATION_COUNT += 1.0


def observe_quality_score(score: float) -> None:
    if score < 0 or score > 1:
        return
    with _LOCK:
        global _QUALITY_SCORE_SUM, _QUALITY_SCORE_COUNT
        _QUALITY_SCORE_SUM += score
        _QUALITY_SCORE_COUNT += 1.0


def observe_batch_size(size: int) -> None:
    if size <= 0:
        return
    with _LOCK:
        global _BATCH_SIZE_SUM, _BATCH_SIZE_COUNT
        _BATCH_SIZE_SUM += float(size)
        _BATCH_SIZE_COUNT += 1.0


def snapshot_metrics() -> dict[str, float]:
    with _LOCK:
        payload = dict(_COUNTERS)
        payload["k8s_diagnosis_duration_seconds_sum"] = _DURATION_SUM
        payload["k8s_diagnosis_duration_seconds_count"] = _DURATION_COUNT
        payload["k8s_diagnosis_duration_seconds"] = (
            (_DURATION_SUM / _DURATION_COUNT) if _DURATION_COUNT else 0.0
        )
        payload["k8s_diagnosis_quality_score_sum"] = _QUALITY_SCORE_SUM
        payload["k8s_diagnosis_quality_score_count"] = _QUALITY_SCORE_COUNT
        payload["k8s_diagnosis_quality_score"] = (
            (_QUALITY_SCORE_SUM / _QUALITY_SCORE_COUNT) if _QUALITY_SCORE_COUNT else 0.0
        )
        payload["k8s_diagnosis_batch_size_sum"] = _BATCH_SIZE_SUM
        payload["k8s_diagnosis_batch_size_count"] = _BATCH_SIZE_COUNT
        payload["k8s_diagnosis_batch_size"] = (
            (_BATCH_SIZE_SUM / _BATCH_SIZE_COUNT) if _BATCH_SIZE_COUNT else 0.0
        )
    return payload


def render_prometheus_metrics() -> str:
    values = snapshot_metrics()
    lines = [
        "# TYPE k8s_diagnosis_requests_total counter",
        f"k8s_diagnosis_requests_total {values['k8s_diagnosis_requests_total']}",
        "# TYPE k8s_diagnosis_fallback_total counter",
        f"k8s_diagnosis_fallback_total {values['k8s_diagnosis_fallback_total']}",
        "# TYPE k8s_diagnosis_duration_seconds gauge",
        f"k8s_diagnosis_duration_seconds {values['k8s_diagnosis_duration_seconds']}",
        "# TYPE k8s_diagnosis_duration_seconds_sum counter",
        f"k8s_diagnosis_duration_seconds_sum {values['k8s_diagnosis_duration_seconds_sum']}",
        "# TYPE k8s_diagnosis_duration_seconds_count counter",
        f"k8s_diagnosis_duration_seconds_count {values['k8s_diagnosis_duration_seconds_count']}",
        "# TYPE k8s_diagnosis_tool_calls_total counter",
        f"k8s_diagnosis_tool_calls_total {values['k8s_diagnosis_tool_calls_total']}",
        "# TYPE k8s_diagnosis_report_upsert_total counter",
        f"k8s_diagnosis_report_upsert_total {values['k8s_diagnosis_report_upsert_total']}",
        "# TYPE k8s_diagnosis_quality_score gauge",
        f"k8s_diagnosis_quality_score {values['k8s_diagnosis_quality_score']}",
        "# TYPE k8s_diagnosis_quality_score_sum counter",
        f"k8s_diagnosis_quality_score_sum {values['k8s_diagnosis_quality_score_sum']}",
        "# TYPE k8s_diagnosis_quality_score_count counter",
        f"k8s_diagnosis_quality_score_count {values['k8s_diagnosis_quality_score_count']}",
        "# TYPE k8s_diagnosis_uncertainty_total counter",
        f"k8s_diagnosis_uncertainty_total {values['k8s_diagnosis_uncertainty_total']}",
        "# TYPE k8s_diagnosis_batch_size gauge",
        f"k8s_diagnosis_batch_size {values['k8s_diagnosis_batch_size']}",
        "# TYPE k8s_diagnosis_batch_size_sum counter",
        f"k8s_diagnosis_batch_size_sum {values['k8s_diagnosis_batch_size_sum']}",
        "# TYPE k8s_diagnosis_batch_size_count counter",
        f"k8s_diagnosis_batch_size_count {values['k8s_diagnosis_batch_size_count']}",
        "# TYPE k8s_diagnosis_batch_report_total counter",
        f"k8s_diagnosis_batch_report_total {values['k8s_diagnosis_batch_report_total']}",
    ]
    return "\n".join(lines) + "\n"


def reset_metrics_for_tests() -> None:
    with _LOCK:
        global _DURATION_SUM, _DURATION_COUNT, _QUALITY_SCORE_SUM, _QUALITY_SCORE_COUNT, _BATCH_SIZE_SUM, _BATCH_SIZE_COUNT
        for key in list(_COUNTERS.keys()):
            _COUNTERS[key] = 0.0
        _DURATION_SUM = 0.0
        _DURATION_COUNT = 0.0
        _QUALITY_SCORE_SUM = 0.0
        _QUALITY_SCORE_COUNT = 0.0
        _BATCH_SIZE_SUM = 0.0
        _BATCH_SIZE_COUNT = 0.0
