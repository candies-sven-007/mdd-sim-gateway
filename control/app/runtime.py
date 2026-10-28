"""Cache of each line's engine runtime, kept fresh from systemd.

One ``systemctl show`` for every configured line every POLL_SECONDS replaces the Docker event
stream: it needs no privileges and costs one short-lived process. A change of state or of
generation (systemd's InvocationID, new on every start) is reported to ``on_change`` the way
Docker events used to be, so a restart systemd performed on its own is still noticed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from . import config as cfg
from . import engine

log = logging.getLogger("mdd.runtime")

POLL_SECONDS = 3.0
# A cached entry older than this is inspected again on demand (the poller normally refreshes
# it long before).
CACHE_SECONDS = 2 * POLL_SECONDS


def _configured_lines() -> list[str]:
    found = []
    for inst in cfg.list_instances():
        iid = str(inst.get("id") or "")
        try:
            engine.line_number(iid)
        except ValueError:
            continue
        found.append(iid)
    return found


class RuntimeRegistry:
    def __init__(self, inspector=engine.unit_runtime, batch=engine.units_runtime,
                 lines=_configured_lines):
        self._inspector = inspector
        self._batch = batch
        self._lines = lines
        self._cache: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._poller: asyncio.Task | None = None
        self._on_change: Callable[[str, dict, str], Awaitable[None]] | None = None

    async def start(self, on_change=None) -> None:
        if self._poller and not self._poller.done():
            return
        self._on_change = on_change
        self._poller = asyncio.create_task(self._poll_forever())

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
            self._poller = None

    async def get(self, iid: str, force: bool = False) -> dict:
        iid = str(iid)
        cached = self._cache.get(iid)
        if not force and cached and time.monotonic() - cached["observed_at"] < CACHE_SECONDS:
            return dict(cached)
        lock = self._locks.setdefault(iid, asyncio.Lock())
        async with lock:
            cached = self._cache.get(iid)
            if not force and cached and time.monotonic() - cached["observed_at"] < CACHE_SECONDS:
                return dict(cached)
            runtime = await asyncio.to_thread(self._inspector, iid)
            runtime = {**runtime, "observed_at": time.monotonic()}
            self._cache[iid] = runtime
            return dict(runtime)

    def invalidate(self, iid: str) -> None:
        self._cache.pop(str(iid), None)

    async def poll_once(self) -> None:
        """Refresh every configured line and report what changed. Public for tests."""
        iids = self._lines()
        if not iids:
            return
        observed = await asyncio.to_thread(self._batch, iids)
        now = time.monotonic()
        for iid in iids:
            runtime = observed.get(iid)
            if runtime is None:
                continue
            previous = self._cache.get(iid)
            self._cache[iid] = {**runtime, "observed_at": now}
            if previous is None:
                continue
            changed = (bool(previous.get("running")) != bool(runtime.get("running"))
                       or previous.get("generation") != runtime.get("generation"))
            if changed and self._on_change:
                action = "start" if runtime.get("running") else "die"
                await self._on_change(iid, dict(self._cache[iid]), action)

    async def _poll_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001 - the next pass retries
                log.warning("engine state poll failed: %s", exc)
            await asyncio.sleep(POLL_SECONDS)
