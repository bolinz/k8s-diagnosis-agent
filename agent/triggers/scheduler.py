from __future__ import annotations

import threading
import time
from typing import Callable


class Scheduler(threading.Thread):
    def __init__(self, interval_seconds: int, callback: Callable[[], None]) -> None:
        super().__init__(daemon=True)
        self.interval_seconds = interval_seconds
        self.callback = callback
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.callback()
            self._stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()
