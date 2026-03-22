from __future__ import annotations

import json
from http import HTTPStatus
from typing import Callable
from wsgiref.simple_server import make_server


def serve_webhook(port: int, callback: Callable[[dict], None]) -> None:
    def app(environ, start_response):
        if environ.get("PATH_INFO") != "/alert" or environ.get("REQUEST_METHOD") != "POST":
            start_response(f"{HTTPStatus.NOT_FOUND.value} Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        content_length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(content_length)
        payload = json.loads(body or b"{}")
        callback(payload)
        start_response(f"{HTTPStatus.ACCEPTED.value} Accepted", [("Content-Type", "application/json")])
        return [json.dumps({"accepted": True}).encode("utf-8")]

    with make_server("", port, app) as server:
        server.serve_forever()
