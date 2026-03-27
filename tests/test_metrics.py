from __future__ import annotations

from agent.metrics import (
    inc_counter,
    observe_diagnosis_duration,
    render_prometheus_metrics,
    reset_metrics_for_tests,
)


def test_render_prometheus_metrics_contains_required_series():
    reset_metrics_for_tests()
    inc_counter("diagnosis_requests_total")
    inc_counter("diagnosis_fallback_total")
    inc_counter("tool_calls_total", 2)
    inc_counter("report_upsert_total")
    observe_diagnosis_duration(0.25)
    payload = render_prometheus_metrics()
    assert "diagnosis_requests_total" in payload
    assert "diagnosis_fallback_total" in payload
    assert "diagnosis_duration_seconds " in payload
    assert "tool_calls_total" in payload
    assert "report_upsert_total" in payload
