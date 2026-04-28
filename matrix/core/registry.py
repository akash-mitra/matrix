"""Discover agents from agents/<name>/agent.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class AgentConfig:
    name: str
    description: str
    provider: str
    model: str
    system_prompt: str
    permission_mode: str
    allowed_tools: list[str]
    cwd: Path
    threads_path: Path
    owner: str
    raw: dict


def load_agents(agents_dir: Path) -> list[AgentConfig]:
    if not agents_dir.is_dir():
        return []
    out: list[AgentConfig] = []
    for child in sorted(agents_dir.iterdir()):
        if not child.is_dir():
            continue
        cfg_path = child / "agent.yaml"
        if not cfg_path.is_file():
            continue
        out.append(_parse(child, cfg_path))
    return out


def _parse(agent_dir: Path, cfg_path: Path) -> AgentConfig:
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    prompt_path = agent_dir / raw["system_prompt"]
    work_dir = agent_dir / raw.get("work_dir", "work")
    return AgentConfig(
        name=raw["name"],
        description=raw.get("description", ""),
        provider=raw["provider"],
        model=raw.get("model", ""),
        system_prompt=prompt_path.read_text(encoding="utf-8"),
        permission_mode=raw.get("permission_mode", "bypassPermissions"),
        allowed_tools=list(raw.get("allowed_tools", [])),
        cwd=work_dir.resolve(),
        threads_path=agent_dir / "threads.json",
        owner=raw.get("owner", "default"),
        raw=raw,
    )
