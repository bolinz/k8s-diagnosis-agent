from __future__ import annotations

from threading import Lock


_LOCK = Lock()
_COUNTERS: dict[str, float] = {
    "diagnosis_requests_total": 0.0,
    "diagnosis_fallback_total": 0.0,
    "tool_calls_total": 0.0,
    "report_upsert_total": 0.0,
}
_DURATION_SUM = 0.0
_DURATION_COUNT = 0.0


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


def snapshot_metrics() -> dict[str, float]:
    with _LOCK:
        payload = dict(_COUNTERS)
        payload["diagnosis_duration_seconds_sum"] = _DURATION_SUM
        payload["diagnosis_duration_seconds_count"] = _DURATION_COUNT
        payload["diagnosis_duration_seconds"] = (
            (_DURATION_SUM / _DURATION_COUNT) if _DURATION_COUNT else 0.0
        )
    return payload


def render_prometheus_metrics() -> str:
    values = snapshot_metrics()
    lines = [
        "# TYPE diagnosis_requests_total counter",
        f"diagnosis_requests_total {values['diagnosis_requests_total']}",
        "# TYPE diagnosis_fallback_total counter",
        f"diagnosis_fallback_total {values['diagnosis_fallback_total']}",
        "# TYPE diagnosis_duration_seconds gauge",
        f"diagnosis_duration_seconds {values['diagnosis_duration_seconds']}",
        "# TYPE diagnosis_duration_seconds_sum counter",
        f"diagnosis_duration_seconds_sum {values['diagnosis_duration_seconds_sum']}",
        "# TYPE diagnosis_duration_seconds_count counter",
        f"diagnosis_duration_seconds_count {values['diagnosis_duration_seconds_count']}",
        "# TYPE tool_calls_total counter",
        f"tool_calls_total {values['tool_calls_total']}",
        "# TYPE report_upsert_total counter",
        f"report_upsert_total {values['report_upsert_total']}",
    ]
    return "\n".join(lines) + "\n"


def reset_metrics_for_tests() -> None:
    with _LOCK:
        global _DURATION_SUM, _DURATION_COUNT
        for key in list(_COUNTERS.keys()):
            _COUNTERS[key] = 0.0
        _DURATION_SUM = 0.0
        _DURATION_COUNT = 0.0
