from __future__ import annotations

import argparse
import logging
import threading
import time

from agent.config.settings import Settings
from agent.runtime_logging import get_logger, log_event, setup_logging
from agent.service import build_runtime_service
from agent.triggers.event_watcher import EventWatcher
from agent.triggers.scheduler import Scheduler
from agent.ui.http_server import serve_http


LOGGER = get_logger("app")


def main() -> None:
    parser = argparse.ArgumentParser(prog="k8s-diagnosis-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run")
    subparsers.add_parser("run-once")
    args = parser.parse_args()

    settings = Settings.from_env()
    setup_logging(settings.log_level)
    log_event(
        LOGGER,
        logging.INFO,
        "startup",
        "starting k8s diagnosis agent",
        provider=settings.model_provider,
        model=settings.ollama_model if settings.model_provider == "ollama" else settings.openai_model,
        base_url=settings.ollama_base_url if settings.model_provider == "ollama" else settings.api_base_url,
        cluster=settings.cluster_name,
        report_namespace=settings.report_namespace,
    )
    service = build_runtime_service(settings)

    if args.command == "run-once":
        service.scan_once()
        return

    scheduler = Scheduler(settings.scan_interval_seconds, service.scan_once)
    scheduler.start()
    event_watcher = EventWatcher(
        stream_factory=service.client.watch_events,
        cluster_name=settings.cluster_name,
        report_namespace=settings.report_namespace,
        workload_name=settings.workload_name,
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
