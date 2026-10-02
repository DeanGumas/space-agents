"""The background asyncio "core" thread and the event bus that pushes updates to the UI.

pywebview owns the main thread (required by both WebView2 and Cocoa), so all async work (agent runs, streaming,
later the scheduler and network clients) lives on one event loop in a dedicated thread.

Events flow core → UI in batches: `emit()` may be called from any thread; a flusher on the core loop sends
everything queued in the last ~50 ms as one JavaScript call. Consecutive `message.delta` events for the same
message are merged so token streaming doesn't flood the bridge.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger(__name__)


class Core:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="space-core", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        """Schedule a coroutine on the core loop from any thread."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self, timeout: float = 3.0) -> None:
        async def _shutdown() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.loop.is_running():
            try:
                self.submit(_shutdown()).result(timeout)
            except Exception:  # noqa: BLE001 - best effort on shutdown
                log.exception("Error while shutting down core tasks")
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout)


class EventBus:
    def __init__(self, core: Core, flush_interval: float = 0.05) -> None:
        self._core = core
        self._interval = flush_interval
        self._pending: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._sink: Callable[[str], None] | None = None
        self._task: concurrent.futures.Future[Any] | None = None

    def start(self) -> None:
        self._task = self._core.submit(self._run())

    def attach(self, sink: Callable[[str], None]) -> None:
        """Start delivering events. `sink` receives a JSON array of events. Events emitted earlier are kept."""
        self._sink = sink

    def emit(self, type_: str, **data: Any) -> None:
        with self._lock:
            self._pending.append({"type": type_, **data})

    def _take_batch(self) -> list[dict[str, Any]]:
        with self._lock:
            batch, self._pending = self._pending, []
        merged: list[dict[str, Any]] = []
        for ev in batch:
            prev = merged[-1] if merged else None
            if (
                prev
                and ev["type"] == prev["type"] == "message.delta"
                and ev.get("message_id") == prev.get("message_id")
            ):
                prev["delta"] += ev["delta"]
            else:
                merged.append(dict(ev))
        return merged

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._interval)
            if self._sink is None:
                continue
            batch = self._take_batch()
            if not batch:
                continue
            payload = json.dumps(batch, ensure_ascii=False)
            try:
                # The sink calls into the GUI thread; keep it off the event loop.
                await loop.run_in_executor(None, self._sink, payload)
            except Exception:  # noqa: BLE001 - a UI hiccup must not kill the flusher
                log.exception("Failed to deliver %d event(s) to the UI", len(batch))
