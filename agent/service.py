from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
import logging
from typing import Any

from agent.analyzers.rules import RuleEngine
from agent.config.settings import Settings
from agent.diagnosis.diagnosis_engine import DiagnosisEngine
from agent.diagnosis.diagnosis_enricher import DiagnosisEnricher
from agent.k8s_client.base import KubernetesReadClient
from agent.metrics import inc_counter, observe_diagnosis_duration, observe_quality_score, observe_batch_size
from agent.models import DiagnosisResult, PendingFinding, TriggerContext, WorkloadRef
from agent.orchestrator.diagnosis_agent import DiagnosisAgent
from agent.orchestrator.responses_client import (
    ModelClient,
    OllamaResponsesClient,
    OpenAIResponsesClient,
)
from agent.reporting.diagnosis_reporter import DiagnosisReportFormatter, KubernetesDiagnosisReportWriter
from agent.reporting.report_writer import ReportWriter
from agent.runtime_logging import get_logger, log_event
from agent.tools.registry import ToolRegistry
from agent.transformers.event_storm_deduper import EventStormDeduper
from agent.transformers.trigger_transformer import TriggerTransformer


LOGGER = get_logger("agent_service")


@dataclass
class AgentService:
    """Kubernetes diagnosis agent facade.

    Delegates to specialized components:
    - TriggerTransformer: normalize, augment, correlate
    - DiagnosisEngine: LLM diagnosis loop
    - DiagnosisEnricher: fill missing fields, quality score, evidence attribution
    - ReportWriter: persist reports
    - EventStormDeduper: suppress duplicate events within a time window
    """

    settings: Settings
    client: KubernetesReadClient
    codex_agent: DiagnosisAgent
    report_writer: object | None = None
    formatter: DiagnosisReportFormatter = field(default_factory=DiagnosisReportFormatter)

    # Internal components (initialized in __post_init__)
    _transformer: TriggerTransformer = field(init=False, repr=False)
    _engine: DiagnosisEngine = field(init=False, repr=False)
    _enricher: DiagnosisEnricher = field(init=False, repr=False)
    _writer: ReportWriter = field(init=False, repr=False)
    _deduper: EventStormDeduper = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._transformer = TriggerTransformer(self.client, self.settings.cluster_name)
        self._engine = DiagnosisEngine(self.codex_agent)
        self._enricher = DiagnosisEnricher(self.codex_agent.rule_engine)
        self._writer = ReportWriter(
            report_namespace=self.settings.report_namespace,
            formatter=self.formatter,
            writer=self.report_writer,
            cluster_name=self.settings.cluster_name,
        )
        self._deduper = EventStormDeduper(
            window_seconds=max(1, self.settings.event_dedupe_window_seconds),
        )

    def scan_once(self) -> list[dict]:
        log_event(LOGGER, logging.INFO, "scan_start", "scheduled scan started")
        findings = self._collect_findings()
        results = []
        threshold = self.settings.batch_threshold
        if threshold > 0:
            grouped: dict[tuple[str, str], list[PendingFinding]] = {}
            for finding in findings:
                key = (finding.trigger.workload.namespace, finding.trigger.symptom)
                grouped.setdefault(key, []).append(finding)
            batch_count = 0
            for (namespace, symptom), group in grouped.items():
                if len(group) >= threshold:
                    batch_count += 1
                    results.append(self._process_batch(group))
            individual_count = len(findings) - sum(len(g) for g in grouped.values() if len(g) >= threshold)
            for (namespace, symptom), group in grouped.items():
                if len(group) < threshold:
                    for finding in group:
                        results.append(self.process_trigger(finding.trigger))
            log_event(
                LOGGER,
                logging.INFO,
                "scan_end",
                "scheduled scan finished",
                findings=len(findings),
                reports=len(results),
                batch_reports=batch_count,
                individual_reports=individual_count,
            )
        else:
            for finding in findings:
                results.append(self.process_trigger(finding.trigger))
            log_event(
                LOGGER,
                logging.INFO,
                "scan_end",
                "scheduled scan finished",
                findings=len(findings),
                reports=len(results),
            )
        return results

    def _process_batch(self, group: list[PendingFinding]) -> dict:
        """Create a single aggregated diagnosis report for a group of findings with the same namespace and symptom."""
        if not group:
            raise ValueError("batch group cannot be empty")
        first = group[0]
        trigger = first.trigger
        affected_workloads = [
            {
                "kind": f.trigger.workload.kind,
                "namespace": f.trigger.workload.namespace,
                "name": f.trigger.workload.name,
                "observed_for_seconds": f.trigger.observed_for_seconds,
            }
            for f in group
        ]
        impact_summary = {
            "type": "batch",
            "affected_count": len(affected_workloads),
            "affected_workloads": affected_workloads,
        }
        diagnosis = DiagnosisResult(
            summary=f"Batch diagnosis: {len(affected_workloads)} workloads in namespace '{trigger.workload.namespace}' affected by {trigger.symptom}",
            severity="warning",
            probable_causes=[
                f"{len(affected_workloads)} workloads experiencing the same symptom in namespace '{trigger.workload.namespace}'"
            ],
            evidence=[
                f"batch_size={len(affected_workloads)}",
                f"symptom={trigger.symptom}",
                f"namespace={trigger.workload.namespace}",
            ],
            recommendations=[
                f"Investigate namespace-level issue affecting {trigger.workload.namespace}",
                "Review cluster events and resource quotas for the namespace",
                "Check for node-level issues or network policy misconfiguration",
            ],
            confidence=0.5,
            related_objects=[],
            root_cause_candidates=[],
            evidence_timeline=[],
            impact_summary=impact_summary,
            quality_score={"overall": 0.5, "method": "batch_aggregation"},
            uncertainties=[f"Individual root causes may vary across {len(affected_workloads)} workloads"],
            evidence_attribution=[],
            raw_agent_output={"mode": "batch", "batch_size": len(affected_workloads)},
            used_fallback=True,
        )
        inc_counter("k8s_diagnosis_requests_total")
        inc_counter("k8s_diagnosis_batch_report_total")
        observe_batch_size(len(affected_workloads))
        result = self._writer.write(
            trigger,
            diagnosis,
            self._active_model_name(),
            self.settings.diagnosis_name_prefix,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "batch_report_created",
            "batch aggregated report written",
            namespace=trigger.workload.namespace,
            symptom=trigger.symptom,
            batch_size=len(affected_workloads),
        )
        return result

    def process_alert(self, payload: dict) -> dict:
        workload_ref = payload.get("workloadRef", {})
        workload = workload_ref if isinstance(workload_ref, dict) else {}
        namespace = (
            str(payload.get("namespace", "")).strip()
            or str(workload.get("namespace", "")).strip()
            or "default"
        )
        name = (
            str(payload.get("name", "")).strip()
            or str(payload.get("pod_name", "")).strip()
            or str(workload.get("name", "")).strip()
        )
        kind = (
            str(payload.get("kind", "")).strip()
            or str(workload.get("kind", "")).strip()
            or "Pod"
        )
        symptom = str(payload.get("symptom", "Pending")).strip() or "Pending"
        observed_for_seconds = self._coerce_observed_seconds(
            payload.get("observed_for_seconds", payload.get("observedFor", 0))
        )
        trigger_at = self._parse_trigger_time(payload)
        cluster = str(payload.get("cluster", "")).strip() or self.settings.cluster_name
        log_event(
            LOGGER,
            logging.INFO,
            "alert_received",
            "alert webhook received",
            namespace=namespace,
            workload_name=name or "",
            workload_kind=kind,
            symptom=symptom,
        )
        trigger = TriggerContext(
            source="alert",
            cluster=cluster,
            workload=WorkloadRef(kind=kind, namespace=namespace, name=name),
            symptom=symptom,
            observed_for_seconds=observed_for_seconds,
            trigger_at=trigger_at,
            raw_signal=payload,
        )
        return self.process_trigger(trigger)

    def _coerce_observed_seconds(self, value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                return 0
            if normalized.endswith("s"):
                normalized = normalized[:-1].strip()
            try:
                return max(0, int(float(normalized)))
            except ValueError:
                return 0
        return 0

    def _parse_trigger_time(self, payload: dict[str, Any]) -> datetime:
        raw_value = payload.get("triggerAt") or payload.get("trigger_at")
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if value:
                normalized = value.replace("Z", "+00:00")
                try:
                    parsed = datetime.fromisoformat(normalized)
                    if parsed.tzinfo is None:
                        return parsed.replace(tzinfo=timezone.utc)
                    return parsed
                except ValueError:
                    pass
        return datetime.now(timezone.utc)

    def process_trigger(self, trigger: TriggerContext) -> dict:
        start = perf_counter()
        inc_counter("k8s_diagnosis_requests_total")
        trigger, correlation = self._transformer.transform(trigger)
        log_event(
            LOGGER,
            logging.INFO,
            "trigger_processing",
            "processing trigger",
            source=trigger.source,
            namespace=trigger.workload.namespace,
            workload_kind=trigger.workload.kind,
            workload_name=trigger.workload.name,
            symptom=trigger.symptom,
        )
        registry = ToolRegistry(
            self.client,
            trigger,
            scope_mode=self.settings.scope_mode,
            allowed_namespaces=set(self.settings.scope_allowed_namespaces),
        )
        diagnosis, tool_history = self._engine.diagnose(trigger, registry)
        diagnosis = self._enricher.ensure_complete(trigger, diagnosis, correlation)
        if diagnosis.used_fallback:
            inc_counter("k8s_diagnosis_fallback_total")
        result = self._writer.write(
            trigger,
            diagnosis,
            self._active_model_name(),
            self.settings.diagnosis_name_prefix,
        )
        observe_diagnosis_duration(perf_counter() - start)
        return result

    def process_event_trigger(self, trigger: TriggerContext) -> dict | None:
        key = ":".join(
            [
                trigger.workload.namespace,
                trigger.workload.kind,
                trigger.workload.name,
                trigger.symptom,
            ]
        )
        now = datetime.now(timezone.utc)
        state = self._deduper.next_state(key, now)
        count = int(state.get("count", 0))
        threshold = max(2, self.settings.event_storm_threshold)
        if count == 1:
            return self.process_trigger(trigger)
        if count < threshold:
            log_event(
                LOGGER,
                logging.INFO,
                "event_deduped",
                "skipping duplicate event trigger",
                namespace=trigger.workload.namespace,
                workload_kind=trigger.workload.kind,
                workload_name=trigger.workload.name,
                symptom=trigger.symptom,
                burst_count=count,
                burst_threshold=threshold,
            )
            return None
        if not state.get("aggregated", False):
            self._deduper.mark_aggregated(key)
            log_event(
                LOGGER,
                logging.WARNING,
                "event_storm_aggregated",
                "aggregating event storm into one fallback report",
                namespace=trigger.workload.namespace,
                workload_kind=trigger.workload.kind,
                workload_name=trigger.workload.name,
                symptom=trigger.symptom,
                burst_count=count,
                burst_threshold=threshold,
            )
            return self._process_aggregated_event_trigger(trigger, state)
        log_event(
            LOGGER,
            logging.INFO,
            "event_storm_suppressed",
            "suppressing additional event storm triggers",
            namespace=trigger.workload.namespace,
            workload_kind=trigger.workload.kind,
            workload_name=trigger.workload.name,
            symptom=trigger.symptom,
            burst_count=count,
            burst_threshold=threshold,
        )
        return None

    def _process_aggregated_event_trigger(self, trigger: TriggerContext, state: dict[str, Any]) -> dict:
        first_seen = state.get("first_seen")
        last_seen = state.get("last_seen")
        if isinstance(first_seen, datetime) and isinstance(last_seen, datetime):
            window_seconds = max(1, int((last_seen - first_seen).total_seconds()))
        else:
            window_seconds = max(1, self.settings.event_dedupe_window_seconds)
        aggregated_raw_signal = dict(trigger.raw_signal or {})
        aggregated_raw_signal.update(
            {
                "aggregated": True,
                "stormCount": int(state.get("count", 0)),
                "stormWindowSeconds": window_seconds,
            }
        )
        aggregated_trigger = TriggerContext(
            source=trigger.source,
            cluster=trigger.cluster,
            workload=trigger.workload,
            symptom=trigger.symptom,
            observed_for_seconds=max(trigger.observed_for_seconds, window_seconds),
            trigger_at=trigger.trigger_at,
            raw_signal=aggregated_raw_signal,
            correlation_context=trigger.correlation_context,
        )
        aggregated_trigger, correlation = self._transformer.transform(aggregated_trigger)
        diagnosis = self._enricher.ensure_complete(
            aggregated_trigger,
            self._engine.fallback_diagnosis(
                aggregated_trigger,
                reason="event_storm_aggregated",
                storm_count=int(state.get("count", 0)),
                storm_window_seconds=window_seconds,
            ),
            correlation,
        )
        return self._writer.write(
            aggregated_trigger,
            diagnosis,
            self._active_model_name(),
            self.settings.diagnosis_name_prefix,
        )

    def list_reports(self, params: dict | None = None) -> list[dict]:
        return self._writer.list_reports(self.client, params)

    def get_report(self, name: str) -> dict | None:
        return self._writer.get_report(self.client, name)

    def backfill_incomplete_reports(self) -> list[dict]:
        return self._writer.backfill(
            self.client,
            self._enricher,
            self.codex_agent,
            self._active_model_name(),
            self.settings.diagnosis_name_prefix,
        )

    def _collect_findings(self) -> list[PendingFinding]:
        engine = self.codex_agent.rule_engine
        findings = engine.findings_from_snapshot(self.client.list_anomaly_snapshot())
        return [
            finding
            for finding in findings
            if not self._is_self_workload(
                finding.trigger.workload.namespace,
                finding.trigger.workload.name,
            )
        ]

    def _is_self_workload(self, namespace: str, name: str) -> bool:
        if namespace != self.settings.report_namespace:
            return False
        return bool(name) and (
            name == self.settings.workload_name
            or name.startswith(f"{self.settings.workload_name}-")
        )

    def _active_model_name(self) -> str:
        model = getattr(self.codex_agent, "model", "")
        if isinstance(model, str) and model:
            return model
        return self.settings.openai_model

    def _ensure_complete_diagnosis(
        self,
        trigger: TriggerContext,
        diagnosis: DiagnosisResult,
        correlation: dict[str, Any] | None = None,
    ) -> DiagnosisResult:
        """Backward-compatible wrapper delegating to DiagnosisEnricher.

        Internal use only. Prefer using process_trigger() for normal operations.
        """
        # Use trigger.correlation_context as default correlation if not provided
        effective_correlation = correlation if correlation is not None else (trigger.correlation_context or {})
        return self._enricher.ensure_complete(trigger, diagnosis, effective_correlation)


def build_runtime_service(settings: Settings) -> AgentService:
    from agent.k8s_client.runtime import RuntimeKubernetesClient

    client = RuntimeKubernetesClient(report_namespace=settings.report_namespace)
    responses_client = build_model_client(settings)
    engine = RuleEngine(
        cluster_name=settings.cluster_name,
        min_observation_seconds=settings.min_observation_seconds,
    )
    codex_agent = DiagnosisAgent(
        responses_client=responses_client,
        rule_engine=engine,
        model=responses_client.model,
        max_tool_calls=settings.max_tool_calls,
        max_input_bytes=settings.max_input_bytes,
        max_diagnosis_seconds=settings.max_diagnosis_seconds,
    )
    writer = KubernetesDiagnosisReportWriter(
        report_namespace=settings.report_namespace,
        formatter=DiagnosisReportFormatter(),
    )
    service = AgentService(
        settings=settings,
        client=client,
        codex_agent=codex_agent,
        report_writer=writer,
    )
    return service


def build_model_client(settings: Settings) -> ModelClient:
    provider = settings.model_provider.lower()
    if provider == "openai":
        return OpenAIResponsesClient(
            api_key=settings.openai_api_key or "",
            model=settings.openai_model,
            api_base_url=settings.api_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
    if provider == "ollama":
        if not settings.ollama_model:
            raise ValueError("OLLAMA_MODEL must be set when MODEL_PROVIDER=ollama")
        return OllamaResponsesClient(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
    raise ValueError(f"Unsupported MODEL_PROVIDER: {settings.model_provider}")
