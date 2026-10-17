"""Per-instance browser worker activity, independent from source request permits."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager


class BrowserWorkload:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._waiting = 0
        self._source_active = {}
        self._source_changed = {}

    def snapshot(self):
        with self._lock:
            return {"active_browser_workers": self._active,
                    "waiting_for_browser_workers": self._waiting}

    async def _acquire_source(self, origin, limit, check):
        while True:
            await check()
            with self._lock:
                if self._source_active.get(origin, 0) < limit:
                    self._source_active[origin] = self._source_active.get(origin, 0) + 1
                    return
                changed = self._source_changed.setdefault(origin, asyncio.Event())
                changed.clear()
            try:
                await asyncio.wait_for(changed.wait(), timeout=1)
            except TimeoutError:
                continue

    def _release_source(self, origin):
        with self._lock:
            active = self._source_active[origin] - 1
            if active:
                self._source_active[origin] = active
            else:
                self._source_active.pop(origin, None)
            # Drop idle event-loop-bound wait state after waking its waiters.
            changed = (self._source_changed.get(origin) if active
                       else self._source_changed.pop(origin, None))
            if changed is not None:
                changed.set()

    @asynccontextmanager
    async def slot(self, semaphore, check, *, source_origin=None, per_source_limit=None):
        """Admit a source page before taking global capacity; keep waits cancellable.

        Optional source admission bounds resident product pages by the existing
        source request concurrency. It never grants request permits or changes
        request pacing. One busy source cannot occupy every global browser slot
        while its requests are queued. Limits apply across all calls for an
        origin; different requested limits do not create separate admission pools.
        """
        if source_origin is not None and (not isinstance(per_source_limit, int)
                                          or isinstance(per_source_limit, bool)
                                          or per_source_limit < 1):
            raise ValueError("Source browser admission requires a positive limit")
        acquired = False
        source_acquired = False
        with self._lock:
            self._waiting += 1
        try:
            if source_origin is not None:
                await self._acquire_source(source_origin, per_source_limit, check)
                source_acquired = True
            while True:
                await check()
                try:
                    await asyncio.wait_for(semaphore.acquire(), timeout=1)
                    break
                except TimeoutError:
                    continue
            with self._lock:
                self._waiting -= 1
                self._active += 1
            acquired = True
            try:
                yield
            finally:
                semaphore.release()
        finally:
            with self._lock:
                if acquired:
                    self._active -= 1
                else:
                    self._waiting -= 1
            if source_acquired:
                self._release_source(source_origin)


browser_workload = BrowserWorkload()
