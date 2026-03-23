from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import logging

from agent.runtime_logging import get_logger, log_event
from agent.ui.templates import INDEX_HTML


LOGGER = get_logger("http")


def serve_http(port: int, service) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _write_json(self, payload, status=HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log_event(
                LOGGER,
                logging.INFO,
                "http_response",
                "request handled",
                method=self.command,
                path=self.path,
                status=status.value,
            )

        def _write_html(self, body: str, status=HTTPStatus.OK) -> None:
            data = body.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            log_event(
                LOGGER,
                logging.INFO,
                "http_response",
                "request handled",
                method=self.command,
                path=self.path,
                status=status.value,
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._write_html(INDEX_HTML)
                return
            if parsed.path == "/healthz":
                self._write_json({"ok": True})
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
            self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/alert":
                self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length) or b"{}")
            report = service.process_alert(payload)
            self._write_json(report, status=HTTPStatus.ACCEPTED)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer(("", port), Handler)
    server.serve_forever()
