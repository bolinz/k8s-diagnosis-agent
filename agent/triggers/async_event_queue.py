from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from agent.metrics import inc_counter


class AsyncEventQueue:
    """Event queue with bounded worker pool and backpressure.

    Runs an asyncio event loop in a dedicated thread, decoupling event
    production (EventWatcher, scheduler) from event processing.
    Provides backpressure via bounded queue and graceful degradation
    when queue is full (dropped events counted).

    Usage:
        queue = AsyncEventQueue(
            maxsize=100,
            num_workers=4,
            process_fn=service.process_trigger,
        )
        queue.start()
        # In EventWatcher or scheduler:
        queue.put(trigger)
        # On shutdown:
        queue.stop()
    """

    def __init__(
        self,
        maxsize: int = 100,
        num_workers: int = 4,
        process_fn: Callable[[Any], Any] | None = None,
    ):
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._num_workers = num_workers
        self._process_fn = process_fn
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopping = False
        self._lock = threading.Lock()

    def put(self, item: Any) -> bool:
        """Put an item into the queue from any thread (non-blocking).

        Thread-safe. Returns True if queued, False if dropped (queue full).
        """
        if self._loop is None or self._stopping:
            return False
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            inc_counter("k8s_event_queue_dropped_total")
            return False

    def put_blocking(self, item: Any, timeout: float = 5.0) -> bool:
        """Put an item into the queue with blocking wait.

        Thread-safe. Returns True if queued, False if timed out (dropped).
        """
        if self._loop is None or self._stopping:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._queue.put(item), self._loop
        )
        try:
            future.result(timeout=timeout)
            return True
        except (asyncio.TimeoutError, Exception):
            inc_counter("k8s_event_queue_dropped_total")
            return False

    def _run_loop(self) -> None:
        """Run the asyncio event loop in this thread."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_workers())
        finally:
            self._loop.close()

    async def _run_workers(self) -> None:
        """Run all worker coroutines until stopped."""
        workers = [
            asyncio.create_task(self._worker(self._process_fn))
            for _ in range(self._num_workers)
        ]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self, process_fn: Callable[[Any], Any]) -> None:
        """Worker coroutine that pulls items from queue and processes them."""
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                # Run synchronous process_fn in a thread pool to avoid blocking
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, process_fn, item)
            except Exception:
                # Swallow processing exceptions - they are logged inside process_trigger
                pass
            finally:
                self._queue.task_done()

    def start(self, process_fn: Callable[[Any], Any] | None = None) -> None:
        """Start the worker pool in a dedicated thread.

        Must be called before put(). Safe to call multiple times (idempotent).
        """
        with self._lock:
            if self._started:
                return
            fn = process_fn or self._process_fn
            if fn is None:
                raise ValueError("process_fn must be provided to start() or constructor")
            self._process_fn = fn
            self._loop = asyncio.new_event_loop()
            self._started = True
            self._stopping = False
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop all workers and the event loop thread."""
        with self._lock:
            if not self._started or self._stopping:
                return
            self._stopping = True

        if self._loop is None:
            return

        # Schedule shutdown on the event loop thread
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)

        # Wait for thread to finish
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        with self._lock:
            self._started = False
            self._stopping = False
            self._loop = None

    async def _shutdown(self) -> None:
        """Async shutdown: cancel all workers."""
        for _ in range(self._num_workers):
            self._queue.put_nowait(None)  # Sentinel to stop worker
        await asyncio.sleep(0.1)  # Give workers time to process sentinels

    def qsize(self) -> int:
        """Return approximate queue size."""
        if self._queue is None:
            return 0
        return self._queue.qsize()

    @property
    def is_started(self) -> bool:
        return self._started and not self._stopping
