from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.analyzers.rules import RuleEngine
from agent.config.settings import Settings
from agent.models import TriggerContext, WorkloadRef
from agent.orchestrator.codex_agent import CodexDiagnosisAgent
from agent.reporting.diagnosis_reporter import DiagnosisReportFormatter
from agent.service import AgentService, build_model_client, build_runtime_service
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

    def get_workload_events(self, namespace, kind, name):
        self.calls.append(("get_workload_events", namespace, kind, name))
        return {"items": [{"reason": "ProgressDeadlineExceeded", "message": "Deployment exceeded its progress deadline"}]}

    def get_owner_chain(self, namespace, kind, name):
        self.calls.append(("get_owner_chain", namespace, kind, name))
        if kind == "Pod":
            return {
                "items": [
                    {"kind": "Pod", "namespace": namespace, "name": name},
                    {"kind": "ReplicaSet", "namespace": namespace, "name": "checkout-rs"},
                    {"kind": "Deployment", "namespace": namespace, "name": "checkout"},
                ]
            }
        return {"items": [{"kind": kind, "namespace": namespace, "name": name}]}

    def get_related_events(self, namespace, kind, name):
        self.calls.append(("get_related_events", namespace, kind, name))
        return {
            "items": [
                {
                    "reason": "BackOff" if kind == "Pod" else "ProgressDeadlineExceeded",
                    "message": "Recent warning event",
                    "timestamp": "2026-03-22T05:00:00+00:00",
                }
            ]
        }

    def list_related_pods(self, namespace, kind, name):
        self.calls.append(("list_related_pods", namespace, kind, name))
        return {"items": [{"metadata": {"name": "checkout-abc"}}, {"metadata": {"name": "checkout-def"}}]}

    def get_attached_pvcs(self, namespace, pod_name):
        self.calls.append(("get_attached_pvcs", namespace, pod_name))
        return {
            "items": [
                {
                    "name": "checkout-pvc",
                    "phase": "Pending",
                    "volumeName": "",
                }
            ]
        }

    def get_pvc_dependents(self, namespace, pvc_name):
        self.calls.append(("get_pvc_dependents", namespace, pvc_name))
        return {
            "items": [
                {"kind": "Pod", "namespace": namespace, "name": "checkout-abc", "phase": "Pending"},
                {"kind": "Pod", "namespace": namespace, "name": "checkout-def", "phase": "Pending"},
            ]
        }

    def get_pod_events(self, namespace, pod_name):
        self.calls.append(("get_pod_events", namespace, pod_name))
        return {"items": [{"reason": "BackOff", "message": "Back-off restarting failed container"}]}

    def get_pod_spec_summary(self, namespace, pod_name):
        self.calls.append(("get_pod_spec_summary", namespace, pod_name))
        return {
            "containers": [
                {
                    "name": "checkout",
                    "image": "example/checkout:v1",
                    "command": ["/app/server"],
                    "args": ["--port=8080"],
                    "envFrom": [{"secretRef": {"name": "checkout-env"}}],
                }
            ],
            "volumes": [
                {"name": "config", "configMap": {"name": "checkout-config"}},
                {"name": "data", "persistentVolumeClaim": {"claimName": "checkout-pvc"}},
            ],
            "serviceAccountName": "default",
            "nodeName": "node-a",
        }

    def get_pod_conditions(self, namespace, pod_name):
        self.calls.append(("get_pod_conditions", namespace, pod_name))
        return {
            "phase": "Pending",
            "reason": "Unschedulable",
            "message": "0/3 nodes are available",
            "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
        }

    def get_container_statuses(self, namespace, pod_name):
        self.calls.append(("get_container_statuses", namespace, pod_name))
        return {"items": [{"restart_count": 12, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}

    def get_deployment_status(self, namespace, name):
        self.calls.append(("get_deployment_status", namespace, name))
        return {
            "replicas": 3,
            "updatedReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 2,
            "conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded"}],
        }

    def get_replicaset_status(self, namespace, name):
        self.calls.append(("get_replicaset_status", namespace, name))
        return {
            "replicas": 3,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "conditions": [{"type": "ReplicaFailure", "reason": "FailedCreate"}],
            "ownerReferences": [{"kind": "Deployment", "name": "checkout"}],
        }

    def get_config_refs(self, namespace, kind, name):
        self.calls.append(("get_config_refs", namespace, kind, name))
        return {
            "items": [
                {"type": "Secret", "name": "checkout-env", "namespace": namespace, "optional": False},
                {"type": "ConfigMap", "name": "checkout-config", "namespace": namespace, "optional": False},
            ]
        }

    def get_pvc_status(self, namespace, pvc_name=None, pod_name=None):
        self.calls.append(("get_pvc_status", namespace, pvc_name, pod_name))
        return {
            "items": [
                {
                    "name": pvc_name or "checkout-pvc",
                    "phase": "Pending",
                    "storageClassName": "fast",
                    "capacity": {"storage": "10Gi"},
                    "volumeName": "",
                }
            ]
        }

    def get_recent_logs(self, namespace, pod_name, container=None):
        self.calls.append(("get_recent_logs", namespace, pod_name, container))
        return {"logs": "panic: missing DATABASE_URL"}

    def get_node_conditions(self, node_name=None):
        return {"items": []}

    def get_node_workload_impact(self, node_name):
        self.calls.append(("get_node_workload_impact", node_name))
        return {
            "node": node_name,
            "podCount": 2,
            "crossNamespace": False,
            "items": [
                {"kind": "Pod", "namespace": "payments", "name": "checkout-abc", "phase": "Pending"},
                {"kind": "Pod", "namespace": "payments", "name": "checkout-def", "phase": "Pending"},
            ],
        }

    def get_namespace_quotas(self, namespace):
        return {"items": []}

    def get_hpa_status(self, namespace, target_name=None):
        return {"items": []}

    def get_resource_metrics(self, namespace, name=None):
        return {"status": "unavailable"}

    def search_similar_reports(self, trigger):
        return {"items": []}

    def get_related_reports(self, namespace, kind, name):
        self.calls.append(("get_related_reports", namespace, kind, name))
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
                    "relatedObjects": [
                        {"kind": "Pod", "namespace": "payments", "name": "checkout-abc", "role": "primary"},
                        {"kind": "Deployment", "namespace": "payments", "name": "checkout", "role": "owner"},
                    ],
                    "rootCauseCandidates": [
                        {
                            "objectRef": {"kind": "Deployment", "namespace": "payments", "name": "checkout"},
                            "reason": "Shared deployment rollout is broken.",
                            "confidence": 0.82,
                        }
                    ],
                    "evidenceTimeline": [
                        {
                            "time": "2026-03-22T05:00:00+00:00",
                            "objectRef": {"kind": "Pod", "namespace": "payments", "name": "checkout-abc"},
                            "signal": "BackOff",
                        }
                    ],
                    "impactSummary": {
                        "workloadCount": 1,
                        "podCount": 1,
                        "crossNamespace": False,
                        "relatedReportCount": 0,
                    },
                    "analysisVersion": "0.3.0",
                    "modelInfo": {"name": "gpt-5-codex", "fallback": False},
                    "rawSignal": {
                        "reason": "BackOff",
                        "message": "restart",
                        "timestamp": "2026-03-22T05:00:00+00:00",
                        "podPhase": "Running",
                        "containerReason": "CrashLoopBackOff",
                    },
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
        model_provider="openai",
        openai_api_key="key",
        openai_model="gpt-5-codex",
        ollama_model="llama3.1",
        cluster_name="prod",
        report_namespace="k8s-diagnosis-system",
        scan_interval_seconds=300,
        min_observation_seconds=600,
        webhook_port=8080,
        max_tool_calls=8,
        max_input_bytes=20000,
        request_timeout_seconds=30,
        api_base_url="https://api.openai.com/v1",
        ollama_base_url="http://127.0.0.1:11434",
        diagnosis_name_prefix="diagnosis",
        event_dedupe_window_seconds=300,
        workload_name="k8s-diagnosis-agent",
        log_level="INFO",
    )


def build_fallback_agent() -> CodexDiagnosisAgent:
    client = FakeResponsesClient([])
    client.api_key = ""
    return CodexDiagnosisAgent(
        responses_client=client,
        rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )


def test_build_model_client_selects_openai_and_ollama():
    openai_client = build_model_client(build_settings())
    assert openai_client.provider_name == "openai"

    settings = build_settings()
    settings.model_provider = "ollama"
    ollama_client = build_model_client(settings)
    assert ollama_client.provider_name == "ollama"
    assert ollama_client.model == "llama3.1"


def test_build_model_client_rejects_invalid_provider():
    settings = build_settings()
    settings.model_provider = "invalid"
    try:
        build_model_client(settings)
    except ValueError as exc:
        assert "Unsupported MODEL_PROVIDER" in str(exc)
    else:
        raise AssertionError("expected invalid provider to raise")


def test_build_runtime_service_uses_selected_provider_model(monkeypatch):
    settings = build_settings()
    settings.model_provider = "ollama"
    settings.ollama_model = "qwen3:8b"

    class FakeRuntimeClient:
        def __init__(self, report_namespace):
            self.report_namespace = report_namespace

    class FakeReportWriter:
        def __init__(self, report_namespace, formatter):
            self.report_namespace = report_namespace
            self.formatter = formatter

    monkeypatch.setattr(
        "agent.k8s_client.runtime.RuntimeKubernetesClient",
        FakeRuntimeClient,
    )
    monkeypatch.setattr(
        "agent.service.KubernetesDiagnosisReportWriter",
        FakeReportWriter,
    )

    service = build_runtime_service(settings)

    assert service.codex_agent.model == "qwen3:8b"
    assert service.codex_agent.responses_client.provider_name == "ollama"


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


def test_tool_registry_exposes_new_read_only_tools_without_secret_values():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CreateContainerConfigError",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)
    config_refs = json.loads(
        registry.execute(
            "get_config_refs",
            {"namespace": "payments", "kind": "Pod", "name": "checkout-abc"},
        )
    )
    pvc_status = json.loads(
        registry.execute(
            "get_pvc_status",
            {"namespace": "payments", "pod_name": "checkout-abc"},
        )
    )
    owner_chain = json.loads(
        registry.execute(
            "get_owner_chain",
            {"namespace": "payments", "kind": "Pod", "name": "checkout-abc"},
        )
    )
    node_impact = json.loads(
        registry.execute(
            "get_node_workload_impact",
            {"node_name": "node-a"},
        )
    )
    assert config_refs["items"][0]["name"] == "checkout-env"
    assert "value" not in json.dumps(config_refs)
    assert pvc_status["items"][0]["phase"] == "Pending"
    assert owner_chain["items"][-1]["kind"] == "Deployment"
    assert node_impact["podCount"] == 2


def test_tool_registry_serializes_datetime_values():
    client = FakeKubernetesClient()
    client.get_workload_status = lambda namespace, kind, name: {
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "timestamp": datetime(2026, 3, 23, 12, 0, tzinfo=timezone.utc),
    }
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)
    payload = json.loads(
        registry.execute(
            "get_workload_status",
            {"namespace": "payments", "kind": "Pod", "name": "checkout-abc"},
        )
    )

    assert payload["timestamp"] == "2026-03-23T12:00:00+00:00"


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


def test_codex_agent_prompt_contains_preferred_tool_order_for_symptom():
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([]),
        rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    prompt = agent._build_user_prompt(
        TriggerContext(
            source="scheduled",
            cluster="prod",
            workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
            symptom="ProgressDeadlineExceeded",
            observed_for_seconds=1800,
        )
    )
    assert "Preferred tool order" in prompt
    assert "get_deployment_status" in prompt
    assert "get_workload_events" in prompt


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
    assert report["analysisVersion"] == "0.3.0"
    assert report["rawSignal"]["podPhase"] == "Running"
    assert report["relatedObjects"][0]["role"] == "primary"
    assert report["rootCauseCandidates"][0]["objectRef"]["kind"] == "Deployment"
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
    assert trigger.raw_signal["involvedObject"]["name"] == "checkout-abc"
    assert trigger.raw_signal["reason"] == "BackOff"
    assert trigger.raw_signal["timestamp"] == "2026-03-22T05:00:00+00:00"

    failed_mount = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "FailedMount",
            "message": "Unable to attach or mount volumes",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            },
            "lastTimestamp": "2026-03-22T05:00:00Z",
        },
    )
    assert failed_mount is not None
    assert failed_mount.symptom == "FailedMount"

    cannot_run = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "Failed",
            "message": "container cannot run: executable file not found",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            },
        },
    )
    assert cannot_run is not None
    assert cannot_run.symptom == "ContainerCannotRun"

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


def test_event_mapping_skips_events_without_object_name():
    trigger = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "",
            },
        },
    )
    assert trigger is None


def test_event_watcher_ignores_agent_self_events():
    watcher = __import__("agent.triggers.event_watcher", fromlist=["EventWatcher"]).EventWatcher(
        stream_factory=lambda: iter(()),
        cluster_name="prod",
        report_namespace="k8s-diagnosis-system",
        workload_name="k8s-diagnosis-agent",
        on_trigger=lambda _trigger: None,
    )
    assert watcher._should_ignore_event(
        {
            "involvedObject": {
                "kind": "Deployment",
                "namespace": "k8s-diagnosis-system",
                "name": "k8s-diagnosis-agent",
            }
        }
    ) is True
    assert watcher._should_ignore_event(
        {
            "involvedObject": {
                "kind": "Pod",
                "namespace": "k8s-diagnosis-system",
                "name": "k8s-diagnosis-agent-abc123",
            }
        }
    ) is True
    assert watcher._should_ignore_event(
        {
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            }
        }
    ) is False


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


def test_service_normalizes_report_metadata_without_unknown_placeholders():
    class SparseClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-sparse"},
                    "spec": {
                        "source": "event",
                        "cluster": "",
                        "namespace": "",
                        "symptom": "CrashLoopBackOff",
                        "observedFor": 0,
                        "triggerAt": "2026-03-22T05:00:00+00:00",
                        "workloadRef": {"kind": "", "name": ""},
                    },
                    "status": {
                        "severity": "warning",
                        "summary": "Detected CrashLoopBackOff.",
                        "probableCauses": ["Crash loop detected."],
                        "evidence": ["reason=BackOff"],
                        "recommendations": ["Inspect logs."],
                        "confidence": 0.6,
                        "analysisVersion": "0.3.0",
                        "modelInfo": {},
                        "rawSignal": {
                            "reason": "BackOff",
                            "message": "Back-off restarting failed container",
                            "timestamp": "2026-03-22T05:00:00+00:00",
                            "involvedObject": {
                                "kind": "Pod",
                                "name": "checkout-abc",
                                "namespace": "payments",
                            },
                        },
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
    report = service.list_reports()[0]
    assert report["workload"]["kind"] == "Pod"
    assert report["workload"]["name"] == "checkout-abc"
    assert report["namespace"] == "payments"
    assert report["cluster"] == "prod"
    assert report["modelInfo"]["name"] == "gpt-5-codex"
    assert report["rawSignal"]["reason"] == "BackOff"


def test_process_trigger_normalizes_event_metadata_before_writing():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=CodexDiagnosisAgent(
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
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="",
            workload=WorkloadRef(kind="", namespace="", name=""),
            symptom="CrashLoopBackOff",
            observed_for_seconds=0,
            raw_signal={
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
            },
        )
    )
    assert report["spec"]["cluster"] == "prod"
    assert report["spec"]["workloadRef"]["kind"] == "Pod"
    assert report["spec"]["workloadRef"]["name"] == ""
    assert report["status"]["modelInfo"]["name"] == "gpt-5-codex"
    assert report["status"]["rawSignal"]["reason"] == "BackOff"
    assert report["status"]["rawSignal"]["timestamp"]


def test_process_trigger_builds_correlation_for_pvc_propagation():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
            symptom="FailedMount",
            observed_for_seconds=1200,
            raw_signal={"reason": "FailedMount", "message": "Unable to attach or mount volumes"},
        )
    )
    assert any(item["kind"] == "PVC" for item in report["status"]["relatedObjects"])
    assert report["status"]["rootCauseCandidates"][0]["objectRef"]["kind"] == "PVC"
    assert report["status"]["impactSummary"]["podCount"] >= 2


def test_process_trigger_builds_correlation_for_node_scoped_impact():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    report = service.process_trigger(
        TriggerContext(
            source="scheduled",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
            symptom="Pending",
            observed_for_seconds=1800,
        )
    )
    assert any(item["kind"] == "Node" for item in report["status"]["relatedObjects"])
    assert any(
        item["objectRef"]["kind"] == "Node"
        for item in report["status"]["rootCauseCandidates"]
    )


def test_process_trigger_builds_correlation_for_rollout_stuck():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    report = service.process_trigger(
        TriggerContext(
            source="scheduled",
            cluster="prod",
            workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
            symptom="ProgressDeadlineExceeded",
            observed_for_seconds=1800,
        )
    )
    assert report["status"]["rootCauseCandidates"][0]["objectRef"]["kind"] == "Deployment"
    assert report["status"]["evidenceTimeline"][0]["signal"]


def test_process_trigger_builds_correlation_for_shared_image_breakage():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
            symptom="ImagePullBackOff",
            observed_for_seconds=900,
            raw_signal={"reason": "Failed", "message": "image pull failed"},
        )
    )
    assert any(
        item["objectRef"]["kind"] in {"Deployment", "ReplicaSet"}
        for item in report["status"]["rootCauseCandidates"]
    )


def test_process_trigger_enriches_raw_signal_for_pod_symptoms():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient(
                [
                    {
                        "output_text": json.dumps(
                            {
                                "summary": "Volume mount is blocked by an unbound PVC.",
                                "severity": "critical",
                                "probableCauses": ["PVC is not bound."],
                                "evidence": ["PVC phase is Pending."],
                                "recommendations": ["Bind or recreate the PVC."],
                                "confidence": 0.81,
                            }
                        )
                    }
                ]
            ),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
            symptom="FailedMount",
            observed_for_seconds=0,
            raw_signal={"reason": "FailedMount", "message": "Unable to attach or mount volumes"},
        )
    )
    assert report["status"]["rawSignal"]["podPhase"] == "Pending"
    assert report["status"]["rawSignal"]["podReason"] == "Unschedulable"
    assert report["status"]["rawSignal"]["containerReason"] == "CrashLoopBackOff"
    assert report["status"]["rawSignal"]["pvcPhase"] == "Pending"


def test_process_trigger_enriches_raw_signal_for_deployment_symptoms():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient(
                [
                    {
                        "output_text": json.dumps(
                            {
                                "summary": "Rollout is stalled.",
                                "severity": "critical",
                                "probableCauses": ["Deployment exceeded its progress deadline."],
                                "evidence": ["Deployment condition reports ProgressDeadlineExceeded."],
                                "recommendations": ["Inspect rollout health."],
                                "confidence": 0.7,
                            }
                        )
                    }
                ]
            ),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    report = service.process_trigger(
        TriggerContext(
            source="scheduled",
            cluster="prod",
            workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
            symptom="ProgressDeadlineExceeded",
            observed_for_seconds=1800,
        )
    )
    assert report["status"]["rawSignal"]["deploymentCondition"] == "ProgressDeadlineExceeded"


def test_scan_once_filters_agent_self_findings():
    class SelfFindingClient(FakeKubernetesClient):
        def list_anomaly_snapshot(self):
            return [
                {
                    "namespace": "k8s-diagnosis-system",
                    "name": "k8s-diagnosis-agent-abc123",
                    "kind": "Pod",
                    "symptom": "Pending",
                    "observed_for_seconds": 1800,
                },
                {
                    "namespace": "payments",
                    "name": "checkout-abc",
                    "kind": "Pod",
                    "symptom": "CrashLoopBackOff",
                    "observed_for_seconds": 1800,
                },
            ]

    service = AgentService(
        settings=build_settings(),
        client=SelfFindingClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient(
                [
                    {
                        "output_text": json.dumps(
                            {
                                "summary": "Checkout pod crashes because DATABASE_URL is missing.",
                                "severity": "critical",
                                "probableCauses": ["Required database environment variable is absent."],
                                "evidence": ["Container logs contain missing DATABASE_URL."],
                                "recommendations": ["Restore secret wiring."],
                                "confidence": 0.93,
                            }
                        )
                    }
                ]
            ),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    reports = service.scan_once()
    assert len(reports) == 1
    assert reports[0]["spec"]["workloadRef"]["name"] == "checkout-abc"


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
