from __future__ import annotations

import json
from datetime import timezone

from agent.analyzers.rules import RuleEngine
from agent.config.settings import Settings
from agent.models import TriggerContext, WorkloadRef
from agent.orchestrator.codex_agent import CodexDiagnosisAgent
from agent.reporting.diagnosis_reporter import DiagnosisReportFormatter
from agent.service import AgentService
from agent.tools.registry import ToolRegistry
from agent.triggers.event_watcher import map_event_to_trigger


class FakeResponsesClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.api_key = "test-key"

    def create_response(self, payload):
        return self.outputs.pop(0)


class FakeKubernetesClient:
    def __init__(self):
        self.calls = []

    def list_anomaly_snapshot(self):
        return [
            {
                "namespace": "payments",
                "name": "checkout-abc",
                "kind": "Pod",
                "symptom": "CrashLoopBackOff",
                "observed_for_seconds": 1800,
            }
        ]

    def get_workload_status(self, namespace, kind, name):
        self.calls.append(("get_workload_status", namespace, kind, name))
        return {"namespace": namespace, "kind": kind, "name": name, "phase": "Running"}

    def list_related_pods(self, namespace, kind, name):
        self.calls.append(("list_related_pods", namespace, kind, name))
        return {"items": [{"metadata": {"name": name}}]}

    def get_pod_events(self, namespace, pod_name):
        self.calls.append(("get_pod_events", namespace, pod_name))
        return {"items": [{"reason": "BackOff", "message": "Back-off restarting failed container"}]}

    def get_container_statuses(self, namespace, pod_name):
        self.calls.append(("get_container_statuses", namespace, pod_name))
        return {"items": [{"restart_count": 12, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}

    def get_recent_logs(self, namespace, pod_name, container=None):
        self.calls.append(("get_recent_logs", namespace, pod_name, container))
        return {"logs": "panic: missing DATABASE_URL"}

    def get_node_conditions(self, node_name=None):
        return {"items": []}

    def get_namespace_quotas(self, namespace):
        return {"items": []}

    def get_hpa_status(self, namespace, target_name=None):
        return {"items": []}

    def get_resource_metrics(self, namespace, name=None):
        return {"status": "unavailable"}

    def search_similar_reports(self, trigger):
        return {"items": []}

    def list_reports(self):
        return [
            {
                "metadata": {"name": "diagnosis-a"},
                "spec": {
                    "source": "event",
                    "cluster": "prod",
                    "namespace": "payments",
                    "symptom": "CrashLoopBackOff",
                    "observedFor": 0,
                    "triggerAt": "2026-03-22T04:59:00+00:00",
                    "workloadRef": {"kind": "Pod", "name": "checkout-abc"},
                },
                "status": {
                    "severity": "critical",
                    "summary": "Checkout pod crashes because DATABASE_URL is missing.",
                    "probableCauses": ["Required database environment variable is absent."],
                    "evidence": ["Container logs contain missing DATABASE_URL."],
                    "recommendations": ["Restore secret wiring."],
                    "confidence": 0.93,
                    "analysisVersion": "0.1.0",
                    "modelInfo": {"name": "gpt-5-codex", "fallback": False},
                    "rawSignal": {"reason": "BackOff", "message": "restart", "timestamp": "2026-03-22T05:00:00+00:00"},
                    "lastAnalyzedAt": "2026-03-22T05:00:00+00:00",
                },
            }
        ]

    def get_report(self, name):
        if name == "diagnosis-a":
            return self.list_reports()[0]
        return None

    def watch_events(self):
        return iter([])


def build_settings() -> Settings:
    return Settings(
        openai_api_key="key",
        openai_model="gpt-5-codex",
        cluster_name="prod",
        report_namespace="k8s-diagnosis-system",
        scan_interval_seconds=300,
        min_observation_seconds=600,
        webhook_port=8080,
        max_tool_calls=8,
        max_input_bytes=20000,
        request_timeout_seconds=30,
        api_base_url="https://api.openai.com/v1",
        diagnosis_name_prefix="diagnosis",
        event_dedupe_window_seconds=300,
    )


def test_tool_registry_executes_registered_tools():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)
    output = registry.execute(
        "get_recent_logs",
        {"namespace": "payments", "pod_name": "checkout-abc"},
    )
    payload = json.loads(output)
    assert "DATABASE_URL" in payload["logs"]


def test_codex_agent_handles_function_call_then_structured_result():
    response_one = {
        "output": [
            {
                "type": "function_call",
                "name": "get_recent_logs",
                "call_id": "call_1",
                "arguments": "{\"namespace\":\"payments\",\"pod_name\":\"checkout-abc\"}",
            }
        ]
    }
    response_two = {
        "output_text": json.dumps(
            {
                "summary": "Checkout pod crashes because DATABASE_URL is missing.",
                "severity": "critical",
                "probableCauses": ["Required database environment variable is absent."],
                "evidence": ["Container logs contain 'missing DATABASE_URL'."],
                "recommendations": ["Restore the secret or env wiring for DATABASE_URL."],
                "confidence": 0.93,
            }
        )
    }
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=1800,
    )
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([response_one, response_two]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    registry = ToolRegistry(client, trigger)
    diagnosis = agent.diagnose(trigger, registry)
    assert diagnosis.summary.startswith("Checkout pod crashes")
    assert diagnosis.confidence == 0.93
    assert client.calls[0][0] == "get_recent_logs"


def test_codex_agent_falls_back_when_model_output_is_invalid():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="ImagePullBackOff",
        observed_for_seconds=1800,
    )
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([{"output_text": "not-json"}]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    diagnosis = agent.diagnose(trigger, ToolRegistry(client, trigger))
    assert diagnosis.used_fallback is True
    assert diagnosis.severity == "critical"


def test_agent_service_scan_once_emits_structured_report_body():
    client = FakeKubernetesClient()
    response = {
        "output_text": json.dumps(
            {
                "summary": "Pod restarts are caused by repeated probe failures.",
                "severity": "warning",
                "probableCauses": ["The readiness endpoint returns failures during startup."],
                "evidence": ["Recent events show repeated BackOff warnings."],
                "recommendations": ["Increase probe initialDelaySeconds and verify the endpoint."],
                "confidence": 0.74,
            }
        )
    }
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([response]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    reports = service.scan_once()
    assert len(reports) == 1
    report = reports[0]
    assert report["kind"] == "DiagnosisReport"
    assert report["status"]["summary"].startswith("Pod restarts")
    assert report["spec"]["symptom"] == "CrashLoopBackOff"


def test_formatter_dedupes_report_name_and_targets_report_namespace_shape():
    formatter = DiagnosisReportFormatter()
    trigger = TriggerContext(
        source="alert",
        cluster="prod",
        workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
        symptom="Pending",
        observed_for_seconds=1900,
    )
    name = formatter.dedupe_name(trigger, "diagnosis")
    body = formatter.build_spec(
        name,
        "k8s-diagnosis-system",
        trigger,
    )
    status = formatter.build_status(engine_result(), "gpt-5-codex", raw_signal={"reason": "BackOff"})
    assert body["metadata"]["name"].startswith("diagnosis-")
    assert body["metadata"]["namespace"] == "k8s-diagnosis-system"
    assert body["spec"]["triggerAt"].endswith("+00:00")
    assert status["modelInfo"]["name"] == "gpt-5-codex"
    assert status["rawSignal"]["reason"] == "BackOff"


def test_service_lists_and_reads_reports():
    client = FakeKubernetesClient()
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    reports = service.list_reports()
    assert reports[0]["name"] == "diagnosis-a"
    assert reports[0]["severity"] == "critical"
    report = service.get_report("diagnosis-a")
    assert report["workload"]["name"] == "checkout-abc"
    assert report["cluster"] == "prod"
    assert report["triggerAt"]
    assert report["analysisVersion"] == "0.1.0"
    assert service.get_report("missing") is None


def test_event_mapping_and_dedupe():
    trigger = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            },
            "lastTimestamp": "2026-03-22T05:00:00Z",
        },
    )
    assert trigger is not None
    assert trigger.symptom == "CrashLoopBackOff"

    client = FakeKubernetesClient()
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient(
            [
                {
                    "output_text": json.dumps(
                        {
                            "summary": "Pod is repeatedly restarting.",
                            "severity": "warning",
                            "probableCauses": ["Repeated crash loop detected."],
                            "evidence": ["Warning event BackOff observed."],
                            "recommendations": ["Inspect container logs."],
                            "confidence": 0.7,
                        }
                    )
                }
            ]
        ),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    first = service.process_event_trigger(trigger)
    second = service.process_event_trigger(trigger)
    assert first is not None
    assert second is None


def test_service_fills_empty_diagnosis_fields_from_fallback():
    client = FakeKubernetesClient()
    settings = build_settings()
    trigger = TriggerContext(
        source="event",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=0,
        raw_signal={"reason": "BackOff", "message": "Back-off restarting failed container", "timestamp": "2026-03-22T05:00:00+00:00"},
    )
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient(
            [
                {
                    "output_text": json.dumps(
                        {
                            "summary": "",
                            "severity": "warning",
                            "probableCauses": [],
                            "evidence": [],
                            "recommendations": [],
                            "confidence": 0.0,
                        }
                    )
                }
            ]
        ),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    diagnosis = service._ensure_complete_diagnosis(trigger, agent.diagnose(trigger, ToolRegistry(client, trigger)))
    assert diagnosis.summary.startswith("Detected CrashLoopBackOff")
    assert diagnosis.evidence
    assert diagnosis.recommendations


def test_backfill_rewrites_incomplete_reports():
    class FakeWriter:
        def __init__(self):
            self.calls = []

        def upsert_report(self, trigger, diagnosis, model, prefix):
            self.calls.append((trigger, diagnosis, model, prefix))
            return {"ok": True}

    class IncompleteClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-b"},
                    "spec": {
                        "source": "scheduled",
                        "cluster": "prod",
                        "namespace": "payments",
                        "symptom": "Pending",
                        "observedFor": 1200,
                        "triggerAt": "2026-03-22T05:00:00+00:00",
                        "workloadRef": {"kind": "Pod", "name": "checkout-abc"},
                    },
                    "status": {"summary": "", "evidence": []},
                }
            ]

    client = IncompleteClient()
    writer = FakeWriter()
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
        report_writer=writer,
    )
    updated = service.backfill_incomplete_reports()
    assert updated == [{"ok": True}]
    assert writer.calls[0][1].summary
    assert writer.calls[0][1].evidence


def engine_result():
    from agent.models import DiagnosisResult

    return DiagnosisResult(
        summary="Scheduler cannot place the pod due to quota pressure.",
        severity="warning",
        probable_causes=["Namespace quota is exhausted."],
        evidence=["Quota usage is near limit."],
        recommendations=["Increase quota or reduce requests."],
        confidence=0.8,
    )
