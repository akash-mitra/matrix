"""Harness — owns workers, inboxes, session manager. Routes envelopes.

Workers are spawned lazily per (agent, user_id) on first submission. The
agent.owner gets a worker pre-spawned at start() so the first user message
has no warmup delay. Future user_ids spawn their own worker on demand.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from matrix.core.agent import AgentWorker
from matrix.core.envelope import Envelope
from matrix.core.inbox import InMemoryInbox
from matrix.core.registry import AgentConfig, load_agents
from matrix.core.session_manager import SessionManager
from matrix.core.threads import Threads
from matrix.providers.base import Provider
from matrix.providers.claude_code import ClaudeCodeProvider

log = logging.getLogger(__name__)


class Harness:
    def __init__(self, agents_dir: Path) -> None:
        self.agents_dir = agents_dir
        self.session_manager = SessionManager()
        self._agent_configs: dict[str, AgentConfig] = {}
        self._providers: dict[str, Provider] = {}
        self._threads: dict[str, Threads] = {}
        self._inboxes: dict[tuple[str, str], InMemoryInbox] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def discover(self) -> None:
        configs = load_agents(self.agents_dir)
        if not configs:
            raise RuntimeError(f"No agents found in {self.agents_dir}")
        for cfg in configs:
            self._agent_configs[cfg.name] = cfg
            self._providers[cfg.name] = self._build_provider(cfg.provider)
            self._threads[cfg.name] = Threads(cfg.threads_path)
        log.info("discovered agents: %s", list(self._agent_configs))

    def list_agents(self) -> list[AgentConfig]:
        return list(self._agent_configs.values())

    def get_agent(self, name: str) -> AgentConfig:
        return self._agent_configs[name]

    def threads_for(self, agent: str) -> Threads:
        return self._threads[agent]

    async def submit(self, envelope: Envelope) -> None:
        if envelope.agent not in self._agent_configs:
            raise KeyError(f"unknown agent: {envelope.agent}")
        await self._ensure_worker(envelope.agent, envelope.user_id)
        await self._inboxes[(envelope.agent, envelope.user_id)].put(envelope)

    async def start(self) -> None:
        for cfg in self._agent_configs.values():
            await self._ensure_worker(cfg.name, cfg.owner)

    async def stop(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _ensure_worker(self, agent: str, user_id: str) -> None:
        key = (agent, user_id)
        async with self._lock:
            if key in self._tasks:
                return
            cfg = self._agent_configs[agent]
            inbox = InMemoryInbox()
            worker = AgentWorker(
                config=cfg,
                provider=self._providers[agent],
                inbox=inbox,
                session_manager=self.session_manager,
                threads=self._threads[agent],
            )
            self._inboxes[key] = inbox
            self._tasks[key] = asyncio.create_task(
                worker.run(), name=f"worker:{agent}:{user_id}"
            )
            log.info("spawned worker for (%s, %s)", agent, user_id)

    @staticmethod
    def _build_provider(name: str) -> Provider:
        if name == "claude_code":
            return ClaudeCodeProvider()
        raise ValueError(f"unknown provider: {name}")
