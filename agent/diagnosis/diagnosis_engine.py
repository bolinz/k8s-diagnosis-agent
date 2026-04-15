from __future__ import annotations

from typing import Any

from agent.models import DiagnosisResult, ToolCallRecord, TriggerContext
from agent.orchestrator.diagnosis_agent import DiagnosisAgent
from agent.tools.registry import ToolRegistry


class DiagnosisEngine:
    """Thin wrapper around DiagnosisAgent.

    Encapsulates the LLM diagnosis loop. Returns (result, tool_history)
    so the caller manages tool history state rather than the agent instance.
    """

    def __init__(self, agent: DiagnosisAgent):
        self._agent = agent

    def diagnose(
        self,
        trigger: TriggerContext,
        tool_registry: ToolRegistry,
    ) -> tuple[DiagnosisResult, list[ToolCallRecord]]:
        """Run diagnosis. Returns (result, tool_history) tuple.

        Tool history is returned rather than stored on the agent instance,
        enabling stateless composition and easier testing.
        """
        return self._agent.diagnose(trigger, tool_registry)

    def fallback_diagnosis(
        self,
        trigger: TriggerContext,
        reason: str = "",
        **kwargs: Any,
    ) -> DiagnosisResult:
        """Run fallback diagnosis without LLM."""
        return self._agent.fallback_diagnosis(trigger, reason, **kwargs)
