from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import logging
import threading
from typing import Any
from uuid import uuid4

from agent.metrics import render_prometheus_metrics
from agent.runtime_logging import get_logger, log_event
from agent.ui.templates import INDEX_HTML


LOGGER = get_logger("http")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _extract_report_name(report: Any) -> str:
    if not isinstance(report, dict):
        return ""
    metadata = report.get("metadata", {})
    if isinstance(metadata, dict):
        return str(metadata.get("name", "")).strip()
    return ""


def _resolve_frontend_dist_dir() -> Path:
    return Path(__file__).resolve().parent / "frontend_dist"


class AlertTaskManager:
    def __init__(self, service: Any, max_workers: int = 2) -> None:
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="alert-worker")
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid4().hex[:12]
        task = {
            "requestId": request_id,
            "status": "queued",
            "createdAt": _utc_now_iso(),
            "startedAt": "",
            "finishedAt": "",
            "error": "",
            "reportName": "",
            "result": {},
        }
        with self._lock:
            self._tasks[request_id] = task
        self.executor.submit(self._run_task, request_id, payload)
        return self.get(request_id) or {"requestId": request_id, "status": "queued"}

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(request_id)
            if task is None:
                return None
            return dict(task)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _run_task(self, request_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            task = self._tasks.get(request_id)
            if task is None:
                return
            task["status"] = "running"
            task["startedAt"] = _utc_now_iso()
        try:
            report = self.service.process_alert(payload)
            with self._lock:
                task = self._tasks.get(request_id)
                if task is None:
                    return
                task["status"] = "succeeded"
                task["finishedAt"] = _utc_now_iso()
                task["result"] = report if isinstance(report, dict) else {}
                task["reportName"] = _extract_report_name(task["result"])
            log_event(
                LOGGER,
                logging.INFO,
                "alert_task_completed",
                "asynchronous alert diagnosis completed",
                request_id=request_id,
                report_name=_extract_report_name(report),
            )
        except Exception as exc:
            with self._lock:
                task = self._tasks.get(request_id)
                if task is None:
                    return
                task["status"] = "failed"
                task["finishedAt"] = _utc_now_iso()
                task["error"] = str(exc)
            log_event(
                LOGGER,
                logging.ERROR,
                "alert_task_failed",
                "asynchronous alert diagnosis failed",
                request_id=request_id,
                error=str(exc),
            )


def serve_http(port: int, service) -> None:
    alert_tasks = AlertTaskManager(service)
    index_html = INDEX_HTML
    frontend_dist = _resolve_frontend_dist_dir()
    frontend_index = frontend_dist / "index.html"

    class Handler(BaseHTTPRequestHandler):
        def _write_json(self, payload, status=HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self._write_bytes(body, "application/json", status)

        def _write_html(self, body: str, status=HTTPStatus.OK) -> None:
            data = body.encode("utf-8")
            self._write_bytes(data, "text/html; charset=utf-8", status)

        def _write_text(self, body: str, content_type: str, status=HTTPStatus.OK) -> None:
            data = body.encode("utf-8")
            self._write_bytes(data, content_type, status)

        def _write_bytes(self, data: bytes, content_type: str, status: HTTPStatus) -> None:
            try:
                self.send_response(status.value)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "http_client_disconnected",
                    "client disconnected before response write completed",
                    method=self.command,
                    path=self.path,
                    status=status.value,
                )
                return
            log_event(
                LOGGER,
                logging.INFO,
                "http_response",
                "request handled",
                method=self.command,
                path=self.path,
                status=status.value,
            )

        def _write_file(self, path: Path) -> None:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            mime, _ = mimetypes.guess_type(path.name)
            self._write_bytes(data, mime or "application/octet-stream", HTTPStatus.OK)

        def _serve_frontend_asset(self, request_path: str) -> bool:
            if not frontend_dist.exists():
                return False
            if request_path == "/":
                if frontend_index.exists():
                    self._write_file(frontend_index)
                    return True
                return False
            clean = request_path.lstrip("/")
            asset = (frontend_dist / clean).resolve()
            if not str(asset).startswith(str(frontend_dist.resolve())):
                self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return True
            if asset.exists() and asset.is_file():
                self._write_file(asset)
                return True
            return False

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                if self._serve_frontend_asset("/"):
                    return
                self._write_html(index_html)
                return
            if parsed.path.startswith("/assets/") or parsed.path in {"/favicon.svg", "/vite.svg"}:
                if self._serve_frontend_asset(parsed.path):
                    return
            if parsed.path == "/healthz":
                self._write_json({"ok": True})
                return
            if parsed.path == "/metrics":
                self._write_text(
                    render_prometheus_metrics(),
                    "text/plain; version=0.0.4; charset=utf-8",
                )
                return
            if parsed.path == "/api/reports":
                params = parse_qs(parsed.query)
                self._write_json(
                    {"items": service.list_reports(params)},
                    status=HTTPStatus.OK,
                )
                return
            if parsed.path.startswith("/api/reports/"):
                name = parsed.path.rsplit("/", 1)[-1]
                report = service.get_report(name)
                if report is None:
                    self._write_json(
                        {"error": "report not found"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                self._write_json(report, status=HTTPStatus.OK)
                return
            if parsed.path.startswith("/api/alerts/"):
                request_id = parsed.path.rsplit("/", 1)[-1]
                task = alert_tasks.get(request_id)
                if task is None:
                    self._write_json(
                        {"error": "request not found"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                self._write_json(task, status=HTTPStatus.OK)
                return
            self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/alert":
                self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            content_length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(content_length) or b"{}")
            except json.JSONDecodeError:
                self._write_json({"error": "invalid json payload"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(payload, dict):
                self._write_json({"error": "payload must be an object"}, status=HTTPStatus.BAD_REQUEST)
                return
            task = alert_tasks.submit(payload)
            self._write_json(task, status=HTTPStatus.ACCEPTED)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer(("", port), Handler)
    try:
        server.serve_forever()
    finally:
        alert_tasks.shutdown()
