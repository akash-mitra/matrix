"""Message envelope and event vocabulary used across Matrix.

Channels (web, telegram, email, ...) submit Envelopes to the Harness; agent
workers translate provider streams into Events and publish them on the
envelope's reply_topic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    MESSAGE_START = "message.start"
    MESSAGE_DELTA = "message.delta"
    TOOL_USE = "tool.use"
    TOOL_RESULT = "tool.result"
    THINKING = "thinking"
    MESSAGE_END = "message.end"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": str(self.type), "data": self.data}


@dataclass(frozen=True)
class Envelope:
    agent: str
    user_id: str
    session_id: str | None
    content: str
    reply_topic: str
    source_channel: str
    submitted_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    metadata: dict[str, Any] = field(default_factory=dict)
