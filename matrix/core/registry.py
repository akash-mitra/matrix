"""Discover agents from agents/<name>/agent.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Per-agent runtime state lives outside the repo. Each agent's cwd resolves
# to <RUNTIME_ROOT>/<name>/work — deliberately *not* under the matrix git
# root. The `claude` CLI walks up from cwd to find a project key for its
# auto-memory and CLAUDE.md auto-discovery; placing work dirs under the
# matrix repo would let one agent inherit the user's matrix-repo MEMORY.md
# and any other agent's parent context. Provider-level env vars (see
# providers/claude_code.py) belt-and-suspender this; the directory layout
# is the structural guarantee.
RUNTIME_ROOT = Path.home() / ".matrix" / "agents"


@dataclass(frozen=True)
class ClaudeCodeFeatures:
    """Per-agent toggles for Claude Code CLI features Matrix suppresses by default.

    All default to False = matrix-strict (the CLI is treated as a transport,
    not a context provider — see AGENTS.md §4.9). Each flag opts the agent
    *back in* to one specific CLI auto-injection path. Default-off is the
    contract; flipping a flag is an explicit declaration of intent.
    """
    load_auto_memory: bool = False    # CLI walks up cwd → injects nearest MEMORY.md
    load_claude_mds: bool = False     # CLI walks up cwd → injects nearest CLAUDE.md
    load_skills: bool = False         # plugin marketplace skills (skill_listing attachment + Skill tool)
    load_deferred_tools: bool = False # full built-in tool registry (TodoWrite, EnterPlanMode, Monitor, ...)


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
    claude_code: ClaudeCodeFeatures = field(default_factory=ClaudeCodeFeatures)


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
    # `work_dir`: relative paths resolve under RUNTIME_ROOT/<name>/, absolute
    # paths are honored as-is (escape hatch for tests / unusual setups).
    raw_work = Path(raw.get("work_dir", "work"))
    work_dir = raw_work if raw_work.is_absolute() else RUNTIME_ROOT / raw["name"] / raw_work
    cc_raw = raw.get("claude_code") or {}
    claude_code = ClaudeCodeFeatures(
        load_auto_memory=bool(cc_raw.get("load_auto_memory", False)),
        load_claude_mds=bool(cc_raw.get("load_claude_mds", False)),
        load_skills=bool(cc_raw.get("load_skills", False)),
        load_deferred_tools=bool(cc_raw.get("load_deferred_tools", False)),
    )
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
        claude_code=claude_code,
    )
