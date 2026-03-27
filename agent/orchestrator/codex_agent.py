from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from agent.analyzers.rules import RuleEngine
from agent.metrics import inc_counter
from agent.models import DiagnosisResult, ToolCallRecord, TriggerContext
from agent.runtime_logging import get_logger, log_event, truncate_text
from agent.tools.registry import ToolRegistry


LOGGER = get_logger("codex_agent")


DEFAULT_SYSTEM_PROMPT = """You are a Kubernetes reliability analyst.
You can only use the provided read-only tools.
You must not suggest direct kubectl mutations.
Return final output as strict JSON with fields:
summary, severity, probableCauses, evidence, recommendations, confidence.
Optional fields:
relatedObjects, rootCauseCandidates, evidenceTimeline, impactSummary.
Confidence must be a float between 0 and 1.
Evidence and recommendations must be short, concrete strings.
If evidence is insufficient, say so explicitly in summary and lower confidence.
Prefer the recommended tool sequence for the current symptom before using unrelated tools."""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return []


def _normalize_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"critical", "warning", "info"}:
        return text
    if text in {"high", "error"}:
        return "critical"
    if text in {"medium", "warn"}:
        return "warning"
    if text in {"low", "notice"}:
        return "info"
    return "warning"


@dataclass
class CodexDiagnosisAgent:
    responses_client: Any
    rule_engine: RuleEngine
    model: str
    max_tool_calls: int
    max_input_bytes: int
    max_diagnosis_seconds: int = 45
    tool_history: list[ToolCallRecord] = field(default_factory=list)

    def diagnose(
        self,
        trigger: TriggerContext,
        tool_registry: ToolRegistry,
    ) -> DiagnosisResult:
        if (
            getattr(self.responses_client, "provider_name", "openai") == "openai"
            and not getattr(self.responses_client, "api_key", None)
        ):
            log_event(
                LOGGER,
                logging.INFO,
                "fallback_selected",
                "using fallback diagnosis because model credentials are unavailable",
                provider=getattr(self.responses_client, "provider_name", ""),
                model=self.model,
                namespace=trigger.workload.namespace,
                workload_kind=trigger.workload.kind,
                workload_name=trigger.workload.name,
                symptom=trigger.symptom,
            )
            return self.rule_engine.fallback_diagnosis(trigger)

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": DEFAULT_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": self._build_user_prompt(trigger),
            },
        ]

        started_at = perf_counter()
        tool_calls = 0
        while True:
            if perf_counter() - started_at >= self.max_diagnosis_seconds:
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "fallback_selected",
                    "using fallback diagnosis because diagnosis time budget was exceeded",
                    provider=getattr(self.responses_client, "provider_name", ""),
                    model=self.model,
                    max_diagnosis_seconds=self.max_diagnosis_seconds,
                )
                return self.rule_engine.fallback_diagnosis(trigger)
            log_event(
                LOGGER,
                logging.INFO,
                "diagnosis_request",
                "requesting diagnosis response",
                provider=getattr(self.responses_client, "provider_name", ""),
                model=self.model,
                namespace=trigger.workload.namespace,
                workload_kind=trigger.workload.kind,
                workload_name=trigger.workload.name,
                symptom=trigger.symptom,
            )
            try:
                response = self.responses_client.create_response(
                    {
                        "model": self.model,
                        "input": messages,
                        "tools": tool_registry.openai_tools(),
                        "tool_choice": "auto",
                    }
                )
            except Exception as exc:
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "fallback_selected",
                    "using fallback diagnosis because model request failed",
                    provider=getattr(self.responses_client, "provider_name", ""),
                    model=self.model,
                    error=str(exc),
                )
                return self.rule_engine.fallback_diagnosis(trigger)
            output = response.get("output", [])
            function_calls = [
                item for item in output if item.get("type") == "function_call"
            ]
            if not function_calls:
                return self._parse_final_response(response, trigger)
            messages.append(
                {
                    "type": "assistant_tool_calls",
                    "tool_calls": function_calls,
                }
            )
            for call in function_calls:
                if perf_counter() - started_at >= self.max_diagnosis_seconds:
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        "fallback_selected",
                        "using fallback diagnosis because diagnosis time budget was exceeded",
                        provider=getattr(self.responses_client, "provider_name", ""),
                        model=self.model,
                        max_diagnosis_seconds=self.max_diagnosis_seconds,
                    )
                    return self.rule_engine.fallback_diagnosis(trigger)
                if tool_calls >= self.max_tool_calls:
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        "fallback_selected",
                        "using fallback diagnosis because tool call budget was exceeded",
                        provider=getattr(self.responses_client, "provider_name", ""),
                        model=self.model,
                        tool_calls=tool_calls,
                    )
                    return self.rule_engine.fallback_diagnosis(trigger)
                tool_calls += 1
                inc_counter("tool_calls_total")
                arguments = json.loads(call.get("arguments") or "{}")
                log_event(
                    LOGGER,
                    logging.INFO,
                    "tool_call_start",
                    "executing read-only tool",
                    tool_name=call["name"],
                    arguments=arguments,
                )
                tool_output = tool_registry.execute(call["name"], arguments)
                self.tool_history.append(
                    ToolCallRecord(
                        name=call["name"],
                        arguments=arguments,
                        output=tool_output[:5000],
                    )
                )
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": tool_output[: self.max_input_bytes],
                    }
                )
                log_event(
                    LOGGER,
                    logging.INFO,
                    "tool_call_end",
                    "tool execution completed",
                    tool_name=call["name"],
                    output=truncate_text(tool_output, 400),
                )

    def _build_user_prompt(self, trigger: TriggerContext) -> str:
        signal_json = json.dumps(trigger.raw_signal, ensure_ascii=True, sort_keys=True)
        preferred_tools = ", ".join(self._preferred_tool_sequence(trigger.symptom))
        return (
            f"Diagnose a Kubernetes issue.\n"
            f"Cluster: {trigger.cluster}\n"
            f"Namespace: {trigger.workload.namespace}\n"
            f"Workload kind: {trigger.workload.kind}\n"
            f"Workload name: {trigger.workload.name}\n"
            f"Symptom: {trigger.symptom}\n"
            f"Observed for seconds: {trigger.observed_for_seconds}\n"
            f"Raw signal: {signal_json[: self.max_input_bytes]}\n"
            f"Correlation context: {json.dumps(trigger.correlation_context, ensure_ascii=True, sort_keys=True)[: self.max_input_bytes]}\n"
            f"Preferred tool order: {preferred_tools}\n"
            "Use tools to gather evidence before concluding."
        )

    def _preferred_tool_sequence(self, symptom: str) -> list[str]:
        if symptom in {"CrashLoopBackOff", "ContainerCannotRun", "CreateContainerError"}:
            return [
                "get_container_statuses",
                "get_recent_logs",
                "get_pod_spec_summary",
                "get_pod_events",
            ]
        if symptom in {"Pending", "FailedMount", "CreateContainerConfigError"}:
            return [
                "get_pod_conditions",
                "get_pod_events",
                "get_namespace_quotas",
                "get_pvc_status",
                "get_config_refs",
            ]
        if symptom in {"ProgressDeadlineExceeded", "ReplicaMismatch"}:
            return [
                "get_deployment_status",
                "get_workload_events",
                "list_related_pods",
                "get_pod_conditions",
            ]
        return [
            "get_workload_status",
            "list_related_pods",
            "get_pod_events",
            "search_similar_reports",
        ]

    def _parse_final_response(
        self,
        response: dict[str, Any],
        trigger: TriggerContext,
    ) -> DiagnosisResult:
        text = self._extract_output_text(response)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log_event(
                LOGGER,
                logging.WARNING,
                "fallback_selected",
                "using fallback diagnosis because model response was not valid json",
                provider=getattr(self.responses_client, "provider_name", ""),
                model=self.model,
                response_text=truncate_text(text, 400),
            )
            return self.rule_engine.fallback_diagnosis(trigger)
        return DiagnosisResult(
            summary=str(payload.get("summary", "")).strip() or "Diagnosis incomplete",
            severity=_normalize_severity(payload.get("severity", "warning")),
            probable_causes=_string_list(payload.get("probableCauses", [])),
            evidence=_string_list(payload.get("evidence", [])),
            recommendations=_string_list(payload.get("recommendations", [])),
            confidence=float(payload.get("confidence", 0.0)),
            related_objects=[
                item for item in payload.get("relatedObjects", []) if isinstance(item, dict)
            ],
            root_cause_candidates=[
                item for item in payload.get("rootCauseCandidates", []) if isinstance(item, dict)
            ],
            evidence_timeline=[
                item for item in payload.get("evidenceTimeline", []) if isinstance(item, dict)
            ],
            impact_summary=payload.get("impactSummary", {})
            if isinstance(payload.get("impactSummary", {}), dict)
            else {},
            raw_agent_output={"response": response, "text": text},
        )

    def _extract_output_text(self, response: dict[str, Any]) -> str:
        if isinstance(response.get("output_text"), str):
            return response["output_text"]
        chunks: list[str] = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                text = content.get("text")
                if text:
                    chunks.append(text)
        return "\n".join(chunks)
