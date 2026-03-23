from __future__ import annotations

import io
import json
import logging
from unittest.mock import patch

from agent.orchestrator.responses_client import (
    OllamaResponsesClient,
    OpenAIResponsesClient,
)
from agent.runtime_logging import JsonFormatter, get_logger, log_event


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_openai_client_filters_internal_messages():
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeHTTPResponse({"output_text": "{}"})

    client = OpenAIResponsesClient(
        api_key="key",
        model="gpt-5-codex",
        api_base_url="https://api.openai.com/v1",
        timeout_seconds=30,
    )
    payload = {
        "model": "gpt-5-codex",
        "input": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
            {"type": "assistant_tool_calls", "tool_calls": [{"name": "get_pod_events"}]},
            {"type": "function_call_output", "call_id": "call_1", "output": "{}"},
        ],
        "tools": [],
        "tool_choice": "auto",
    }
    with patch("agent.orchestrator.responses_client.request.urlopen", fake_urlopen):
        client.create_response(payload)
    assert captured["url"].endswith("/responses")
    assert len(captured["body"]["input"]) == 3
    assert all(item.get("type") != "assistant_tool_calls" for item in captured["body"]["input"])


def test_ollama_client_maps_tool_call_response():
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeHTTPResponse(
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "get_pod_events",
                                "arguments": {"namespace": "payments", "pod_name": "checkout-abc"},
                            }
                        }
                    ],
                }
            }
        )

    client = OllamaResponsesClient(
        model="llama3.1",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=30,
    )
    payload = {
        "input": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_pod_events",
                "description": "Get pod events",
                "parameters": {"type": "object"},
            }
        ],
        "tool_choice": "auto",
    }
    with patch("agent.orchestrator.responses_client.request.urlopen", fake_urlopen):
        response = client.create_response(payload)
    assert captured["url"].endswith("/api/chat")
    assert captured["body"]["tools"][0]["function"]["name"] == "get_pod_events"
    assert response["output"][0]["type"] == "function_call"
    assert response["output"][0]["name"] == "get_pod_events"


def test_json_logging_outputs_structured_fields_without_secrets():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = get_logger("test_logger")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_event(
        logger,
        logging.INFO,
        "startup",
        "starting",
        provider="ollama",
        model="llama3.1",
        api_key="hidden",
        openai_api_key="hidden",
    )

    payload = json.loads(stream.getvalue().strip())
    assert payload["component"] == "test_logger"
    assert payload["event"] == "startup"
    assert payload["provider"] == "ollama"
    assert "api_key" not in payload
    assert "openai_api_key" not in payload
