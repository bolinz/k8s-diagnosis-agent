from __future__ import annotations

import argparse
import threading
import time

from agent.config.settings import Settings
from agent.service import build_runtime_service
from agent.triggers.event_watcher import EventWatcher
from agent.triggers.scheduler import Scheduler
from agent.ui.http_server import serve_http


def main() -> None:
    parser = argparse.ArgumentParser(prog="k8s-diagnosis-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run")
    subparsers.add_parser("run-once")
    args = parser.parse_args()

    settings = Settings.from_env()
    service = build_runtime_service(settings)

    if args.command == "run-once":
        service.scan_once()
        return

    scheduler = Scheduler(settings.scan_interval_seconds, service.scan_once)
    scheduler.start()
    event_watcher = EventWatcher(
        stream_factory=service.client.watch_events,
        cluster_name=settings.cluster_name,
        on_trigger=service.process_event_trigger,
    )
    event_watcher.start()
    http_thread = threading.Thread(
        target=serve_http,
        args=(settings.webhook_port, service),
        daemon=True,
    )
    http_thread.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.stop()
        event_watcher.stop()


if __name__ == "__main__":
    main()
