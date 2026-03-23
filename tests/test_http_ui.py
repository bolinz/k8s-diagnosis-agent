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
    assert "metadataLine(" in INDEX_HTML
    assert "workloadLabel(" in INDEX_HTML
    assert "Workload Context" in INDEX_HTML
    assert "Key Signals" in INDEX_HTML
    assert "Primary Finding" in INDEX_HTML
    assert "Related Objects" in INDEX_HTML
    assert "Evidence Timeline" in INDEX_HTML


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
    assert detail["rawSignal"]["containerReason"] == "CrashLoopBackOff"
    assert detail["relatedObjects"]
    assert detail["rootCauseCandidates"]
    assert detail["evidenceTimeline"]
    assert detail["impactSummary"]["workloadCount"] == 1


def test_service_does_not_emit_unknown_or_na_placeholders():
    class SparseClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-sparse"},
                    "spec": {
                        "source": "scheduled",
                        "cluster": "",
                        "namespace": "payments",
                        "symptom": "Pending",
                        "observedFor": 0,
                        "triggerAt": "",
                        "workloadRef": {"kind": "Pod", "name": ""},
                    },
                    "status": {
                        "severity": "warning",
                        "summary": "Pending workload detected.",
                        "probableCauses": ["Capacity unavailable."],
                        "evidence": ["Scheduler is blocked."],
                        "recommendations": ["Inspect scheduling constraints."],
                        "confidence": 0.5,
                        "analysisVersion": "0.3.0",
                    },
                }
            ]

    service = AgentService(
        settings=build_settings(),
        client=SparseClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient([]),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    item = service.list_reports()[0]
    assert item["workload"]["name"] == ""
    assert item["modelInfo"]["name"] == "gpt-5-codex"
    assert item["rawSignal"] == {}
