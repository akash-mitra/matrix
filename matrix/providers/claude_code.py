"""Claude Code provider — wraps ``claude-agent-sdk``.

Connect-on-message / disconnect-on-turn-end. New sessions pin ``session_id=``
so the on-disk JSONL transcript matches our id; resumed sessions use
``resume=``.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from matrix.core.envelope import Event, EventType


class ClaudeCliNotFound(RuntimeError):
    pass


class ClaudeCodeProvider:
    def __init__(self, cli_path: str | None = None) -> None:
        resolved = cli_path or shutil.which("claude")
        if not resolved:
            raise ClaudeCliNotFound(
                "`claude` CLI not on PATH. Install Claude Code and run "
                "`claude auth login` before starting Matrix."
            )
        self._cli_path = resolved

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
    ) -> AsyncIterator[Event]:
        cwd.mkdir(parents=True, exist_ok=True)
        options = ClaudeAgentOptions(
            cli_path=self._cli_path,
            cwd=str(cwd),
            permission_mode=permission_mode,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            setting_sources=[],
            session_id=session_id if is_new_session else None,
            resume=None if is_new_session else session_id,
        )
        client = ClaudeSDKClient(options=options)

        yield Event(EventType.MESSAGE_START, {"session_id": session_id})

        try:
            await client.connect()
            await client.query(message)
            async for chunk in client.receive_response():
                for event in _translate(chunk):
                    yield event
        except Exception as exc:  # noqa: BLE001
            yield Event(
                EventType.ERROR,
                {"kind": type(exc).__name__, "message": str(exc)},
            )
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            yield Event(EventType.MESSAGE_END, {"session_id": session_id})


def _translate(chunk: object):
    """Map an SDK message into zero or more Matrix events."""
    if isinstance(chunk, AssistantMessage):
        for block in chunk.content:
            if isinstance(block, TextBlock):
                yield Event(EventType.MESSAGE_DELTA, {"text": block.text})
            elif isinstance(block, ThinkingBlock):
                yield Event(EventType.THINKING, {"text": block.thinking})
            elif isinstance(block, ToolUseBlock):
                yield Event(
                    EventType.TOOL_USE,
                    {"id": block.id, "name": block.name, "input": block.input},
                )
    elif isinstance(chunk, UserMessage):
        # During a turn, UserMessages only appear as carriers for tool_result
        # blocks — surface those, ignore plain text echoes.
        if isinstance(chunk.content, list):
            for block in chunk.content:
                if isinstance(block, ToolResultBlock):
                    yield Event(
                        EventType.TOOL_RESULT,
                        {
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": bool(block.is_error),
                        },
                    )
    elif isinstance(chunk, (SystemMessage, ResultMessage)):
        # Init / final result metadata — not surfaced to the UI in phase 1.
        return
