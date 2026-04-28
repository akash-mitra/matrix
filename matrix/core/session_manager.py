"""In-memory pub/sub for streaming events back to channels.

A reply_topic is an opaque id minted by a channel when it submits an Envelope.
The agent worker publishes Events on that topic; channels subscribe to receive.

A topic is registered at submission time so events published before any
subscriber has attached are buffered. The first subscriber drains that backlog
and then tails live events. After ``close()`` is called and all subscribers
have received the sentinel, the topic record is discarded.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from matrix.core.envelope import Event


@dataclass
class _Topic:
    backlog: list[Event] = field(default_factory=list)
    subscribers: list[asyncio.Queue[Event | None]] = field(default_factory=list)
    closed: bool = False


class SessionManager:
    def __init__(self) -> None:
        self._topics: dict[str, _Topic] = {}
        self._lock = asyncio.Lock()

    async def register(self, topic: str) -> None:
        async with self._lock:
            self._topics.setdefault(topic, _Topic())

    async def subscribe(self, topic: str) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        async with self._lock:
            state = self._topics.setdefault(topic, _Topic())
            for event in state.backlog:
                await queue.put(event)
            state.backlog.clear()
            if state.closed:
                await queue.put(None)
            else:
                state.subscribers.append(queue)

        async def _gen() -> AsyncIterator[Event]:
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        return
                    yield event
            finally:
                async with self._lock:
                    state = self._topics.get(topic)
                    if state is not None and queue in state.subscribers:
                        state.subscribers.remove(queue)
                        if state.closed and not state.subscribers:
                            self._topics.pop(topic, None)

        return _gen()

    async def publish(self, topic: str, event: Event) -> None:
        async with self._lock:
            state = self._topics.setdefault(topic, _Topic())
            if state.closed:
                return
            if not state.subscribers:
                state.backlog.append(event)
                return
            subs = list(state.subscribers)
        for q in subs:
            await q.put(event)

    async def close(self, topic: str) -> None:
        async with self._lock:
            state = self._topics.get(topic)
            if state is None or state.closed:
                return
            state.closed = True
            subs = list(state.subscribers)
            if not subs:
                # Nobody attached and we're done — nothing to deliver later.
                self._topics.pop(topic, None)
        for q in subs:
            await q.put(None)
