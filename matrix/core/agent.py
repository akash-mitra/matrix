"""Long-lived agent worker — owns one inbox, processes one turn at a time."""

from __future__ import annotations

import asyncio
import logging

from matrix.core.envelope import Envelope, Event, EventType
from matrix.core.inbox import Inbox
from matrix.core.registry import AgentConfig
from matrix.core.session_manager import SessionManager
from matrix.core.threads import Threads
from matrix.providers.base import Provider
from matrix.transcripts.reader import transcripts_dir_for

log = logging.getLogger(__name__)


class AgentWorker:
    def __init__(
        self,
        config: AgentConfig,
        provider: Provider,
        inbox: Inbox,
        session_manager: SessionManager,
        threads: Threads,
    ) -> None:
        self.config = config
        self._provider = provider
        self._inbox = inbox
        self._sessions = session_manager
        self._threads = threads

    async def run(self) -> None:
        log.info("agent %s: worker started", self.config.name)
        while True:
            envelope = await self._inbox.get()
            await self._handle(envelope)

    async def _handle(self, env: Envelope) -> None:
        if env.session_id:
            session_id = env.session_id
        else:
            session_id, _ = self._threads.get_or_create(env.user_id)
        # Decide is_new from transcript existence, not from threads.json.
        # A pinned id (rotated default, fresh UUID) or a stale threads.json
        # entry can both reference an id that has no JSONL yet — those must
        # be treated as new sessions for the SDK, not resume targets.
        transcript = transcripts_dir_for(self.config.cwd) / f"{session_id}.jsonl"
        is_new = not transcript.is_file()
        log.info(
            "agent %s: handling turn (user=%s session=%s new=%s)",
            self.config.name, env.user_id, session_id, is_new,
        )
        try:
            async for event in self._provider.run_turn(
                session_id=session_id,
                is_new_session=is_new,
                cwd=self.config.cwd,
                system_prompt=self.config.system_prompt,
                allowed_tools=self.config.allowed_tools,
                permission_mode=self.config.permission_mode,
                model=self.config.model,
                message=env.content,
            ):
                await self._sessions.publish(env.reply_topic, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("agent %s: unhandled error", self.config.name)
            await self._sessions.publish(
                env.reply_topic,
                Event(
                    EventType.ERROR,
                    {"kind": type(exc).__name__, "message": str(exc)},
                ),
            )
        finally:
            await self._sessions.close(env.reply_topic)
