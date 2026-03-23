from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from agent.runtime_logging import get_logger, log_event


LOGGER = get_logger("scheduler")


class Scheduler(threading.Thread):
    def __init__(self, interval_seconds: int, callback: Callable[[], None]) -> None:
        super().__init__(daemon=True)
        self.interval_seconds = interval_seconds
        self.callback = callback
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                log_event(LOGGER, logging.INFO, "tick", "scheduler tick started")
                self.callback()
                log_event(LOGGER, logging.INFO, "tick_complete", "scheduler tick completed")
            except Exception as exc:
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "tick_failed",
                    "scheduler callback failed",
                    error=str(exc),
                )
            self._stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()
