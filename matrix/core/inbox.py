"""Per-agent inbox queue.

Phase 1: in-memory asyncio.Queue. The Inbox protocol is small so a SQLite or
file-backed durable inbox can be dropped in later without touching workers.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from matrix.core.envelope import Envelope


class Inbox(Protocol):
    async def put(self, envelope: Envelope) -> None: ...
    async def get(self) -> Envelope: ...


class InMemoryInbox:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Envelope] = asyncio.Queue()

    async def put(self, envelope: Envelope) -> None:
        await self._queue.put(envelope)

    async def get(self) -> Envelope:
        return await self._queue.get()
