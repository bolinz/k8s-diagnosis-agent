from __future__ import annotations

import time

from agent.ui.http_server import AlertTaskManager, _extract_report_name


class SuccessfulService:
    def process_alert(self, payload):
        return {
            "metadata": {"name": f"diagnosis-{payload.get('name', 'unknown')}"},
            "status": {"summary": "ok"},
        }


class FailingService:
    def process_alert(self, payload):
        raise RuntimeError("simulated failure")


def _wait_for_terminal_state(manager: AlertTaskManager, request_id: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = manager.get(request_id)
        assert task is not None
        if task["status"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("task did not reach terminal state in time")


def test_extract_report_name():
    assert _extract_report_name({"metadata": {"name": "diagnosis-a"}}) == "diagnosis-a"
    assert _extract_report_name({"metadata": {}}) == ""
    assert _extract_report_name({}) == ""


def test_alert_task_manager_success():
    manager = AlertTaskManager(SuccessfulService(), max_workers=1)
    try:
        accepted = manager.submit({"name": "abc"})
        assert accepted["status"] in {"queued", "running", "succeeded"}
        assert accepted["requestId"]
        done = _wait_for_terminal_state(manager, accepted["requestId"])
        assert done["status"] == "succeeded"
        assert done["reportName"] == "diagnosis-abc"
        assert done["result"]["metadata"]["name"] == "diagnosis-abc"
    finally:
        manager.shutdown()


def test_alert_task_manager_failure():
    manager = AlertTaskManager(FailingService(), max_workers=1)
    try:
        accepted = manager.submit({"name": "abc"})
        done = _wait_for_terminal_state(manager, accepted["requestId"])
        assert done["status"] == "failed"
        assert "simulated failure" in done["error"]
    finally:
        manager.shutdown()
