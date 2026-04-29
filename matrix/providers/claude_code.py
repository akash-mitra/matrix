"""Claude Code provider — wraps ``claude-agent-sdk``.

Connect-on-message / disconnect-on-turn-end. New sessions pin ``session_id=``
so the on-disk JSONL transcript matches our id; resumed sessions use
``resume=``.

Per-agent ``ClaudeCodeFeatures`` flip CLI defaults that Matrix suppresses
out of the box (auto-memory, CLAUDE.md auto-discovery, plugin skills,
deferred tool registry). Default-off is the contract — see AGENTS.md §4.9.
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
from matrix.core.registry import ClaudeCodeFeatures


class ClaudeCliNotFound(RuntimeError):
    pass


class ClaudeCodeProvider:
    def __init__(
        self,
        cli_path: str | None = None,
        features: ClaudeCodeFeatures | None = None,
    ) -> None:
        resolved = cli_path or shutil.which("claude")
        if not resolved:
            raise ClaudeCliNotFound(
                "`claude` CLI not on PATH. Install Claude Code and run "
                "`claude auth login` before starting Matrix."
            )
        self._cli_path = resolved
        self._features = features or ClaudeCodeFeatures()

    async def run_turn(
        self,
        *,
        session_id: str,
        is_new_session: bool,
        cwd: Path,
        system_prompt: str,
        allowed_tools: list[str],
        permission_mode: str,
        model: str,
        message: str,
    ) -> AsyncIterator[Event]:
        cwd.mkdir(parents=True, exist_ok=True)
        f = self._features
        # Each CLI auto-injection path is gated by an explicit flag. Default
        # (all-False) sets every DISABLE_* env var, restricts the tool
        # registry to allowed_tools, and doesn't enable the Skill tool.
        # Flipping a flag in agent.yaml's `claude_code:` block opts back in.
        env: dict[str, str] = {}
        if not f.load_auto_memory:
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        if not f.load_claude_mds:
            env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        if not f.load_skills:
            # Suppresses the skill_listing attachment (~2k tokens of installed
            # plugin descriptions). Also drops any other CLI-emitted attachment
            # blocks; Matrix's _translate ignores those anyway.
            env["CLAUDE_CODE_DISABLE_ATTACHMENTS"] = "1"

        # Tool registry: by default match it to the allowlist exactly so the
        # agent doesn't see deferred tools (TodoWrite, EnterPlanMode, Monitor,
        # ...) it can't use. When load_skills=True we also add Skill so the
        # agent can actually invoke listed skills.
        tools_arg: list[str] | None = None
        if not f.load_deferred_tools:
            tools_arg = list(allowed_tools)
            if f.load_skills and "Skill" not in tools_arg:
                tools_arg.append("Skill")

        options = ClaudeAgentOptions(
            cli_path=self._cli_path,
            cwd=str(cwd),
            permission_mode=permission_mode,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            tools=tools_arg,
            skills="all" if f.load_skills else None,
            setting_sources=[],
            # Architectural invariant — not a per-agent toggle. The CLI
            # auto-discovers MCP servers from ~/.claude.json (claude.ai
            # connectors like Gmail/Drive/Calendar) regardless of
            # setting_sources. --strict-mcp-config restricts the registry to
            # servers passed via --mcp-config, which is also how the SDK
            # forwards agent.yaml's `mcp_servers:` block when that ships.
            # Net: only Matrix-declared MCP servers reach an agent.
            extra_args={"strict-mcp-config": None},
            env=env,
            session_id=session_id if is_new_session else None,
            resume=None if is_new_session else session_id,
            model=model or None,
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
