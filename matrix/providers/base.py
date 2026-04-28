"""Provider interface — abstracts a backend that runs an agent turn.

Phase 1 has only ClaudeCodeProvider. Future providers (codex, bedrock, local)
implement the same interface so swapping is a config change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from matrix.core.envelope import Event


class Provider(Protocol):
    async def run_turn(
        self,
        *,
        session_id: str,
        is_new_session: bool,
        cwd: Path,
        system_prompt: str,
        allowed_tools: list[str],
        permission_mode: str,
        message: str,
    ) -> AsyncIterator[Event]: ...
