"""Read past sessions from the `claude` CLI's on-disk JSONL transcripts.

The CLI writes each session to ``~/.claude/projects/<encoded-cwd>/<id>.jsonl``
where ``encoded-cwd`` replaces ``/`` and ``.`` with ``-``. One transcript
directory is shared by every CLI invocation in the same cwd — interactive
``claude`` runs, IDE sessions, and SDK-driven subprocesses all land there. We
filter on ``entrypoint == "sdk-py"`` so the threads list contains only
Matrix-created sessions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_ENTRYPOINT = "sdk-py"
_TRANSCRIPTS_ROOT = Path.home() / ".claude" / "projects"


def transcripts_dir_for(cwd: Path) -> Path:
    encoded = re.sub(r"[/.]", "-", str(cwd))
    return _TRANSCRIPTS_ROOT / encoded


@dataclass(frozen=True)
class ThreadSummary:
    session_id: str
    title: str
    updated_at: datetime
    message_count: int


@dataclass(frozen=True)
class HistoryItem:
    role: str  # "user" | "assistant"
    blocks: list[dict[str, Any]] = field(default_factory=list)


def list_sessions(cwd: Path) -> list[ThreadSummary]:
    directory = transcripts_dir_for(cwd)
    if not directory.is_dir():
        return []
    out: list[ThreadSummary] = []
    for path in directory.glob("*.jsonl"):
        summary = _summarize(path)
        if summary is not None:
            out.append(summary)
    out.sort(key=lambda s: s.updated_at, reverse=True)
    return out


def load_history(cwd: Path, session_id: str) -> list[HistoryItem] | None:
    path = transcripts_dir_for(cwd) / f"{session_id}.jsonl"
    if not path.is_file():
        return None
    items: list[HistoryItem] = []
    for raw in _iter_jsonl(path):
        item = _to_item(raw)
        if item is not None:
            items.append(item)
    return items


def _summarize(path: Path) -> ThreadSummary | None:
    title: str | None = None
    first_user_text: str | None = None
    message_count = 0
    last_ts: datetime | None = None
    is_app_session = False

    for raw in _iter_jsonl(path):
        if raw.get("entrypoint") == APP_ENTRYPOINT:
            is_app_session = True
        t = raw.get("type")
        if t == "ai-title":
            title = raw.get("aiTitle") or title
        elif t == "user":
            text = _extract_user_text(raw)
            if text:
                message_count += 1
                if first_user_text is None:
                    first_user_text = text
                ts = _parse_ts(raw.get("timestamp"))
                if ts and (last_ts is None or ts > last_ts):
                    last_ts = ts
        elif t == "assistant":
            if _has_displayable_assistant_block(raw):
                message_count += 1
            ts = _parse_ts(raw.get("timestamp"))
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts

    if message_count == 0 or not is_app_session:
        return None
    if last_ts is None:
        last_ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    return ThreadSummary(
        session_id=path.stem,
        title=title or _truncate(first_user_text or "(untitled)", 80),
        updated_at=last_ts,
        message_count=message_count,
    )


def _to_item(raw: dict) -> HistoryItem | None:
    t = raw.get("type")
    if t not in ("user", "assistant"):
        return None
    content = raw.get("message", {}).get("content")
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return None
        return HistoryItem(role=t, blocks=[{"type": "text", "text": text}])
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                text = (block.get("text") or "").strip()
                if text:
                    blocks.append({"type": "text", "text": text})
            elif bt == "thinking":
                # Don't surface thinking in replay (matches live UI).
                continue
            elif bt == "tool_use":
                blocks.append({
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input", {}),
                })
            elif bt == "tool_result":
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": block.get("content"),
                    "is_error": bool(block.get("is_error")),
                })
        if not blocks:
            return None
        return HistoryItem(role=t, blocks=blocks)
    return None


def _extract_user_text(raw: dict) -> str:
    content = raw.get("message", {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "".join(parts).strip()
    return ""


def _has_displayable_assistant_block(raw: dict) -> bool:
    content = raw.get("message", {}).get("content")
    if not isinstance(content, list):
        return False
    for b in content:
        if isinstance(b, dict) and b.get("type") in ("text", "tool_use"):
            return True
    return False


def _iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        log.warning("failed reading %s: %s", path, exc)


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _truncate(text: str, n: int) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
