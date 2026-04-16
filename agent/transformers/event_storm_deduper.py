from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any


class EventStormDeduper:
    """Thread-safe event storm deduplication with state machine.

    Tracks event counts, first/last seen timestamps, and aggregated flag
    per key within a configurable window. Used to suppress duplicate
    triggers and aggregate event storms into a single fallback report.
    """

    def __init__(self, window_seconds: int = 60):
        self._timestamps: dict[str, datetime] = {}
        self._counts: dict[str, int] = {}
        self._first_seen: dict[str, datetime] = {}
        self._aggregated: dict[str, bool] = {}
        self._window = window_seconds
        self._lock = threading.Lock()

    def should_process(self, key: str) -> bool:
        """Atomically check-and-set. Returns True if the event should be processed.

        If the key has never been seen or the window has expired, returns True
        and records the current timestamp. Otherwise returns False (deduped).
        """
        with self._lock:
            now = datetime.now(timezone.utc)
            last = self._timestamps.get(key)
            if last is None or (now - last).total_seconds() > self._window:
                self._timestamps[key] = now
                return True
            return False

    def next_state(self, key: str, now: datetime) -> dict[str, Any]:
        """Atomically compute and update state for a key.

        Returns the state dict with count, first_seen, last_seen, aggregated.
        Resets state if window has expired since first_seen.
        """
        with self._lock:
            window = self._window
            first = self._first_seen.get(key)
            if first is None or (now - first).total_seconds() >= window:
                # New or expired window — reset state
                self._first_seen[key] = now
                self._timestamps[key] = now
                self._counts[key] = 1
                self._aggregated[key] = False
            else:
                # Within window — increment
                self._timestamps[key] = now
                self._counts[key] = int(self._counts.get(key, 0)) + 1

            return {
                "first_seen": self._first_seen[key],
                "last_seen": self._timestamps[key],
                "count": self._counts[key],
                "aggregated": self._aggregated[key],
            }

    def mark_aggregated(self, key: str) -> None:
        """Mark a key as already having an aggregated report generated."""
        with self._lock:
            self._aggregated[key] = True

    def get_state(self, key: str) -> dict[str, Any] | None:
        """Get the current state for a key, or None if not tracked."""
        with self._lock:
            if key not in self._timestamps:
                return None
            return {
                "first_seen": self._first_seen.get(key),
                "last_seen": self._timestamps.get(key),
                "count": self._counts.get(key, 0),
                "aggregated": self._aggregated.get(key, False),
            }

    def reset(self, key: str) -> None:
        """Manually reset a key's state."""
        with self._lock:
            self._timestamps.pop(key, None)
            self._counts.pop(key, None)
            self._first_seen.pop(key, None)
            self._aggregated.pop(key, None)

    def reset_expired(self) -> None:
        """Remove expired entries to prevent unbounded memory growth."""
        with self._lock:
            now = datetime.now(timezone.utc)
            expired = [
                k for k, t in self._timestamps.items()
                if (now - t).total_seconds() > self._window
            ]
            for k in expired:
                del self._timestamps[k]
                self._counts.pop(k, None)
                self._first_seen.pop(k, None)
                self._aggregated.pop(k, None)
