# Matrix — Phase 1 Plan

A personal agent harness. Phase 1 ships **one Claude agent reachable from a local web chat UI** with streaming responses, transcript history, and session resume. Everything else is deferred — but the abstractions are sized to absorb later phases without refactoring.

## 1. Scope

**In phase 1**
- One general-purpose Claude agent (`assistant`)
- Web channel only (FastAPI + SSE, served on `127.0.0.1`)
- One provider: `ClaudeCodeProvider` (wraps `claude-agent-sdk`)
- In-memory inbox queues, keyed by `(agent, user_id)`
- Single thread per `(agent, user_id)` — but `session_id` is part of the envelope schema so multi-thread per `(agent, user_id)` drops in later
- Connect-on-message / disconnect-on-turn-end for the SDK client
- Transcripts read directly from `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`
- Single user (`user_id="default"`)

**Explicitly deferred**
- Other providers (OpenAI/Codex, Bedrock, local)
- Other channels (Telegram, email, API)
- Cron, MCP servers, memory tools, compaction
- Orchestrator and sub-agent-as-tool
- Multi-user auth, durable queues, idle-disconnect LRU
- Coding agent (its own phase)

## 2. Repo layout

```
matrix/                       # repo root (this dir)
  pyproject.toml              # uv-managed, python 3.12
  uv.lock
  .python-version             # 3.12
  .gitignore

  matrix/                     # the package
    __init__.py
    __main__.py               # `uv run matrix` entrypoint
    config.py                 # paths, env, defaults

    core/
      envelope.py             # Envelope, Event dataclasses
      inbox.py                # Inbox interface + InMemoryInbox
      session_manager.py      # in-memory pub/sub for reply topics
      agent.py                # AgentWorker (long-lived asyncio task)
      registry.py             # loads agents/*/agent.yaml
      harness.py              # owns workers, session_manager, channels
      threads.py              # default-thread resolver per (agent, user)

    providers/
      base.py                 # Provider protocol
      claude_code.py          # ClaudeCodeProvider

    channels/
      base.py                 # Channel protocol
      web.py                  # FastAPI app + routes + SSE

    web/static/
      index.html
      app.js
      styles.css

    transcripts/
      reader.py               # parse ~/.claude/projects JSONL

  agents/
    assistant/
      agent.yaml
      prompts/system.md
      tools/                  # agent-specific (empty for now)
      skills/                 # agent-specific (empty for now)
      threads.json            # {user_id: session_id} default-thread map (gitignored)
  # Note: per-agent SDK cwd lives at ~/.matrix/agents/<name>/work/ — outside
  # the repo so the CLI's auto-memory / CLAUDE.md walk-up doesn't inherit
  # the matrix repo's developer context. See AGENTS.md §4.9.

  shared/
    tools/                    # cross-agent (empty for now)
    skills/                   # cross-agent (empty for now)

  tests/
    test_envelope.py
    test_session_manager.py
    test_threads.py
    test_transcripts_reader.py
    test_harness_smoke.py     # mock provider end-to-end
```

## 3. Key schemas

### `agent.yaml`

```yaml
name: assistant
description: General-purpose personal assistant
provider: claude_code
model: claude-opus-4-7         # informational; OAuth subscription decides actual
system_prompt: prompts/system.md
permission_mode: bypassPermissions
allowed_tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
  - WebSearch
  - WebFetch
# work_dir defaults to ~/.matrix/agents/<name>/work/. Set explicitly only
# if you need an absolute path (escape hatch for tests). See AGENTS.md §4.9.
owner: default                  # user_id used for system-initiated messages

# Reserved for later phases — schema parser ignores unknown keys for now,
# but these names are committed so configs stay forward-compatible:
# mcp_servers: []
# cron: []
# channels: []
# sub_agents: []
```

### Message envelope

```python
@dataclass(frozen=True)
class Envelope:
    agent: str                  # target agent name
    user_id: str                # "default" in phase 1
    session_id: str | None      # None → resolve default thread for (agent, user_id)
    content: str                # user message text
    reply_topic: str            # channel-defined opaque topic id
    source_channel: str         # "web" | "telegram" | "email" | "cron" | "agent"
    submitted_at: datetime
    metadata: dict              # arbitrary
```

### Event vocabulary (worker → reply_topic)

A small Matrix-internal event type that providers translate the SDK's stream into:

```python
class EventType(StrEnum):
    MESSAGE_START = "message.start"
    MESSAGE_DELTA = "message.delta"     # text chunk
    TOOL_USE     = "tool.use"           # {name, input}
    TOOL_RESULT  = "tool.result"        # {name, output}
    MESSAGE_END  = "message.end"
    ERROR        = "error"
```

This insulates channels from any SDK shape change and lets future providers map their own event streams to the same vocabulary.

## 4. Core interfaces

### Provider

```python
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
```

`ClaudeCodeProvider.run_turn`:
- builds `ClaudeAgentOptions` like an earlier PoC (explicit `cli_path`, `setting_sources=[]`, allowlisted tools, system prompt, `bypassPermissions`)
- passes `session_id=` for new sessions, `resume=session_id` otherwise
- connects, sends `query(message)`, iterates the SDK's stream, translates each chunk into `Event`s, disconnects on completion
- on exception, yields `Event(type=ERROR, ...)` and disconnects

### Inbox

```python
class Inbox(Protocol):
    async def put(self, env: Envelope) -> None: ...
    async def get(self) -> Envelope: ...
```

Phase 1: `InMemoryInbox = asyncio.Queue` wrapper. Swappable later for SQLite-backed durable inbox.

### SessionManager

In-memory pub/sub for streaming events back to channels:

```python
class SessionManager:
    def subscribe(self, topic: str) -> AsyncIterator[Event]: ...
    async def publish(self, topic: str, event: Event) -> None: ...
    async def close(self, topic: str) -> None: ...
```

Implementation: `dict[str, list[asyncio.Queue[Event]]]`. Multiple subscribers per topic supported (e.g. web tab + future Telegram bridge tailing the same conversation).

### AgentWorker

```python
class AgentWorker:
    def __init__(self, config, provider, session_manager, threads): ...
    async def run(self) -> None:
        while True:
            env = await self.inbox.get()
            session_id = self._resolve_session(env)
            is_new = session_id != env.session_id  # newly minted
            try:
                async for event in self.provider.run_turn(
                    session_id=session_id,
                    is_new_session=is_new,
                    cwd=self.config.cwd,
                    system_prompt=self.config.system_prompt,
                    allowed_tools=self.config.allowed_tools,
                    permission_mode=self.config.permission_mode,
                    message=env.content,
                ):
                    await self.session_manager.publish(env.reply_topic, event)
            finally:
                await self.session_manager.close(env.reply_topic)
```

One worker = one inbox = one in-flight turn at a time → strict ordering for the `(agent, user_id)` queue.

### Default-thread resolver

```python
class Threads:
    """Maps (agent, user_id) → session_id; persists to agents/<agent>/threads.json."""
    def get_or_create(self, agent: str, user_id: str) -> tuple[str, bool]: ...
```

Returns `(session_id, is_new)`. Phase 1 uses this when `Envelope.session_id is None`.

### Channel

```python
class Channel(Protocol):
    name: str
    async def start(self, harness: "Harness") -> None: ...
    async def stop(self) -> None: ...
```

Phase 1 has only `WebChannel`, which mounts FastAPI routes onto a uvicorn server.

### Harness

```python
class Harness:
    def __init__(self, agents, session_manager, channels): ...
    async def submit(self, env: Envelope) -> None:
        await self.inboxes[(env.agent, env.user_id)].put(env)
    async def run(self) -> None:
        # start worker tasks, then channel.start() each, then wait
```

## 5. FastAPI routes (web channel)

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/`                                       | serve `index.html` |
| `GET`  | `/api/agents`                             | list agents (name, description) |
| `GET`  | `/api/agents/{name}/threads`              | list past sessions (read transcript dir) |
| `POST` | `/api/agents/{name}/messages`             | submit message; returns `{session_id, reply_topic}` |
| `GET`  | `/api/sessions/{reply_topic}/stream`      | SSE stream of `Event`s |
| `GET`  | `/api/transcripts/{session_id}`           | full transcript for replay |

`POST` body: `{user_id?: str, session_id?: str, content: str}`. If `session_id` omitted, the harness resolves the default thread for `(agent, user_id)`. Server mints a fresh `reply_topic` (UUID) per submission and the client opens the SSE connection to it.

## 6. Web UI (no build step)

Single `index.html` + `app.js` + `styles.css`.
- Left sidebar: agent list (top), past threads for selected agent (below)
- Right pane: chat (transcript renders on thread click, then live messages append)
- Send box: posts to `/api/agents/{name}/messages`, opens `EventSource` on the returned `reply_topic`, appends `message.delta` events as they stream
- Tool use renders as a collapsible block

Vanilla JS or Alpine.js — no React, no bundler, no `npm`.

## 7. Boot & lifecycle

`uv run matrix` (entrypoint `matrix.__main__:main`):
1. Load global config (port, host, paths)
2. Discover `agents/*/agent.yaml` → list of `AgentConfig`
3. Build `SessionManager`, `Threads`
4. For each agent: build `Provider`, `Inbox`, `AgentWorker`; register in `Harness`; start worker task
5. Build `WebChannel`; `await channel.start(harness)` (mounts FastAPI, runs uvicorn)
6. On SIGINT: stop channels, drain workers (cancel pending turns), exit cleanly

## 8. Testing strategy

- **Unit**: envelope (de)serialization, threads resolver round-trip, session_manager pub/sub fan-out, transcript reader against fixture JSONL
- **Integration (mock provider)**: spin up the harness with a fake provider that emits a scripted event stream; POST a message; assert SSE receives the expected events in order
- **Smoke (manual)**: actual `claude` CLI; one happy-path conversation; resume; one tool call

No live-CLI tests in CI (CI doesn't have OAuth credentials).

## 9. Done criteria

- [ ] `uv run matrix` boots on `127.0.0.1:8765`; `assistant` agent ready
- [ ] Browser at `http://127.0.0.1:8765` shows chat UI
- [ ] Send a message → streamed response renders incrementally
- [ ] Tool-use events render as collapsible blocks
- [ ] Refresh page → past threads listed in sidebar (read from `~/.claude/projects/...`)
- [ ] Click a past thread → transcript renders; type to continue (resume works)
- [ ] Ctrl-C shuts everything down cleanly (no orphaned `claude` subprocesses)
- [ ] Adding a second agent = `mkdir agents/foo/{prompts,work} && write agent.yaml + system.md`, no Python changes, picked up on next boot

## 10. Open implementation questions to resolve as we build

- Exact mapping from `claude-agent-sdk` stream events to Matrix `Event` types (will inspect the SDK's actual event shapes during implementation)
- Whether `permission_mode=bypassPermissions` is the right default for the `assistant` agent (a prior PoC uses it; revisit when coding agent lands)
- Whether `tools=` and `allowed_tools=` need to differ in the SDK options (PoC sets both equal — confirm during build)
- Default-thread persistence format: single `threads.json` per agent vs. one file per `(agent, user_id)` — leaning single file
