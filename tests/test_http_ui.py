from __future__ import annotations

from agent.analyzers.rules import RuleEngine
from agent.orchestrator.codex_agent import CodexDiagnosisAgent
from agent.service import AgentService
from agent.ui.templates import INDEX_HTML

from tests.test_agent_service import FakeKubernetesClient, FakeResponsesClient, build_settings


def test_ui_template_contains_expected_sections():
    assert "K8s Diagnosis" in INDEX_HTML
    assert "/api/reports" in INDEX_HTML
    assert "Probable Causes" in INDEX_HTML
    assert 'id="filter-namespace"' in INDEX_HTML
    assert "Fix Suggestions" in INDEX_HTML
    assert "Trigger at:" in INDEX_HTML


def test_service_api_shape_matches_ui_expectations():
    client = FakeKubernetesClient()
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    service = AgentService(
        settings=settings,
        client=client,
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient([]),
            rule_engine=engine,
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )

    reports = service.list_reports()
    assert len(reports) == 1
    item = reports[0]
    assert item["name"] == "diagnosis-a"
    assert item["workload"]["kind"] == "Pod"
    assert item["summary"]
    assert item["severity"] == "critical"
    assert item["cluster"] == "prod"
    assert item["triggerAt"]
    detail = service.get_report("diagnosis-a")
    assert detail["probableCauses"]
    assert detail["recommendations"]
    assert detail["rawSignal"]["reason"] == "BackOff"
