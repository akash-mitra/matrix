# Matrix Concepts

This document walks through the building blocks of Matrix in the order they make sense to learn them, and explains *why* each one exists in the shape it does. Read this once, then keep [README.md](README.md) and [AGENTS.md](AGENTS.md) handy as references.

## 1. The problem we're solving

Matrix is a personal agent harness. The goals — in priority order:

1. **Run several specialized agents** (assistant, stock analyst, social-media manager, coding agent, orchestrator). Each agent has its own persona, prompt, working directory, and tool allowlist.
2. **Reach those agents from multiple channels** (web today; later Telegram, email, programmatic API). The agent should not care which channel a message came from.
3. **Use OAuth subscription auth, not API keys.** The Anthropic Pro/Max subscription is what we have. The `claude` CLI knows how to use it. No `ANTHROPIC_API_KEY` anywhere.
4. **Preserve conversation continuity.** A message I send by email in the morning and a message I send by web in the afternoon should land in the same conversation if I want them to.
5. **Stay tiny and operable.** Personal infra. One Python process, a few hundred lines of glue, files-on-disk for state. No Redis, no SQLite (yet), no Kubernetes, no microservices.

These five constraints explain almost every design choice that follows.

## 2. The two layers

Matrix has two layers, and they are separated cleanly:

- **The harness** — receives messages, routes them to agents, runs turns through providers, streams events back. Knows nothing about HTTP, Telegram, or email.
- **Channels** — speak to the outside world. They produce `Envelope`s to feed the harness, and consume `Event` streams to deliver replies back to the user. They know nothing about how an agent runs.

Everything between those two layers is the harness.

## 3. The pieces, bottom-up

### 3.1 Envelope

The unit of work that flows from a channel to an agent. Defined in [matrix/core/envelope.py](matrix/core/envelope.py):

```python
Envelope(
    agent="assistant",          # which agent to dispatch to
    user_id="default",          # who is sending — produced by the channel
    session_id=None,            # explicit thread, or None to use the default thread
    content="hi",
    reply_topic="<uuid>",       # opaque id; events for this turn publish here
    source_channel="web",       # informational
    submitted_at=...,
    metadata={},
)
```

Two important fields, conceptually:

- **`(agent, user_id)`** — together they identify the queue this envelope lands in. This means a single user's messages to a single agent are always serialized, regardless of which channel they arrive on. Different users (or different agents) run in parallel.
- **`session_id` (optional)** — if the channel pins a specific thread, the worker resumes that thread. Otherwise the worker resolves the default thread for `(agent, user_id)`.

`reply_topic` is minted fresh for every envelope. Events for that turn are published on it; the channel subscribed to it reads them and renders the reply.

### 3.2 Event

What the worker emits as a turn progresses. Also in [envelope.py](matrix/core/envelope.py):

```
EventType: message.start | message.delta | tool.use | tool.result
           | thinking | message.end | error
```

An `Event` is a small Matrix-internal type. Providers translate their backend's stream (the `claude-agent-sdk`'s `AssistantMessage`/`UserMessage`/`ToolUseBlock`/...) into this shared vocabulary. Channels consume `Event`s. This insulates the rest of the system from any one provider's event shape and lets a future OpenAI provider feed the same UI.

### 3.3 Inbox

A queue. One inbox per `(agent, user_id)`. In [matrix/core/inbox.py](matrix/core/inbox.py):

```python
class Inbox(Protocol):
    async def put(self, envelope: Envelope) -> None: ...
    async def get(self) -> Envelope: ...
```

Phase 1 ships `InMemoryInbox`, a thin wrapper over `asyncio.Queue`. The protocol is small on purpose — when we need durability (cron jobs that survive restarts, email arriving while the harness is down), we'll drop in a SQLite- or filesystem-backed inbox without changing anything else.

### 3.4 SessionManager (pub/sub for replies)

The matching half of inbox: where outgoing events go. In [matrix/core/session_manager.py](matrix/core/session_manager.py).

It is *not* the same thing as a "Claude session" — naming inherited from an earlier PoC. A better name would be `EventBus` or `ReplyPubSub`; we'll likely rename it.

Each `reply_topic` is a topic with zero or more subscribers. The worker publishes `Event`s on that topic; channels subscribe to it and stream the events to the client. Two important behaviors:

- **Backlog buffering.** If the worker publishes events before any subscriber attaches (very common — the channel POSTs and only then opens the SSE connection), events are buffered. The first subscriber drains the backlog and then tails live events. This is the cleanest fix for a small but real race.
- **Multiple subscribers per topic.** A future Telegram bridge can tail the same topic as a web tab if both happen to be watching. Phase 1 doesn't exercise this.

When the worker calls `close(topic)`, all subscribers receive a sentinel and the topic is reaped.

### 3.5 Threads

Maps `user_id → session_id` per agent. In [matrix/core/threads.py](matrix/core/threads.py).

An agent's `threads.json` records "the current thread for each user." When a channel submits an envelope without a `session_id`, the worker calls `Threads.get_or_create(user_id)` to find or mint one. When the user clicks "+" in the web UI, the channel calls `Threads.rotate(user_id)` to mint a fresh `session_id` and make it the new default.

This is small but load-bearing for *cross-channel* continuity. An email arriving with no UI context still has a sensible target thread to land in.

### 3.6 Provider

The abstraction over a model backend. In [matrix/providers/base.py](matrix/providers/base.py):

```python
class Provider(Protocol):
    async def run_turn(
        self, *, session_id, is_new_session, cwd,
        system_prompt, allowed_tools, permission_mode, message,
    ) -> AsyncIterator[Event]: ...
```

Phase 1 has one implementation — [`ClaudeCodeProvider`](matrix/providers/claude_code.py). It wraps `claude-agent-sdk`'s `ClaudeSDKClient`:

- Connects on the message, disconnects on turn end. Simple, no client lifecycle bookkeeping.
- Pins `session_id=` for new sessions (so the on-disk JSONL filename matches our id) or `resume=` for continuations.
- Sets `setting_sources=[]` to fully detach from Claude Code's `CLAUDE.md` / settings discovery — Matrix is *not* Claude Code, just a CLI we drive.
- Translates each SDK chunk (`AssistantMessage`, `UserMessage` carrying `tool_result`s, etc.) into Matrix `Event`s.

A future `CodexProvider` would do the same against the `codex` CLI; a future `BedrockProvider` would speak the API directly. **We do not use LiteLLM** — it routes API keys, which doesn't help with subscription OAuth. The provider abstraction is the right seam.

### 3.7 AgentWorker

The long-lived task that owns one inbox and turns its envelopes into events. In [matrix/core/agent.py](matrix/core/agent.py).

```
loop:
    envelope = await inbox.get()
    session_id = resolve(envelope)            # explicit, or default-thread, or new
    is_new = (no transcript on disk for that id)
    async for event in provider.run_turn(...):
        await session_manager.publish(envelope.reply_topic, event)
    await session_manager.close(envelope.reply_topic)
```

One worker per `(agent, user_id)`. Strict serial within that pair. This is the *concurrency contract*: two messages from the same user to the same agent run one at a time.

The `is_new` detection deserves a callout. The default flow is: the channel either supplies a `session_id` (resume an existing thread) or supplies none (use the default). But "+" in the UI mints a fresh `session_id` and pins it as the new default — then the next message arrives with that pinned id. The worker checks whether `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl` exists; if not, it's a brand-new session and the SDK is told `session_id=` instead of `resume=`. Without this check, fresh-id-on-disk-doesn't-exist-yet becomes a SDK error.

### 3.8 AgentConfig + Registry

Each agent is a directory under `agents/` (config in repo) plus a runtime working directory under `~/.matrix/agents/<name>/` (state on disk, outside repo):

```
agents/<name>/                   # in-repo (config + auto-managed thread map)
  agent.yaml                     # declarative config
  prompts/system.md              # system prompt loaded by the provider
  threads.json                   # auto-managed
  tools/  skills/                # agent-specific (empty in phase 1)

~/.matrix/agents/<name>/         # out-of-repo (per-agent runtime state)
  work/                          # cwd handed to the provider; transcripts encode from this
```

`work/` is deliberately outside the matrix repo. The `claude` CLI walks up from cwd to find a project key for its auto-memory and CLAUDE.md auto-discovery; placing per-agent work dirs under the matrix git root would let every agent inherit the matrix repo's developer-mode context (and each other's, once we have multiple). The provider also gates the CLI's other auto-injection paths (plugin skills, deferred tools, claude.ai MCP connectors) — see §5 *"Why Matrix owns context, not the CLI"*. Per-agent overrides live in the `claude_code:` block.

`agent.yaml` is loaded by [matrix/core/registry.py](matrix/core/registry.py) into an `AgentConfig`. The schema reserves names for future fields (`mcp_servers`, `cron`, `channels`, `sub_agents`) so configs stay forward-compatible even as the parser ignores them today.

Adding a new agent is a directory and a config — no Python changes — provided its provider already exists.

### 3.9 Harness

The top-level container. In [matrix/core/harness.py](matrix/core/harness.py).

It owns the `SessionManager`, the `AgentConfig` registry, one `Threads` per agent, and the dynamic worker pool keyed by `(agent, user_id)`. Workers spawn lazily on first submission for that pair; the `agent.owner` user gets a worker pre-spawned at startup so the first message has no warmup delay.

`harness.submit(envelope)` is the only entry point channels need.

### 3.10 Channels

A channel is anything that produces envelopes and consumes reply topics. In [matrix/channels/base.py](matrix/channels/base.py):

```python
class Channel(Protocol):
    name: str
    async def start(self, harness): ...
    async def stop(self): ...
```

Phase 1 has [WebChannel](matrix/channels/web.py): a FastAPI app mounted on uvicorn. Routes:

- `GET /` — serves the static UI.
- `GET /api/agents` — list of agents.
- `GET /api/agents/{agent}/threads` — past threads for that agent.
- `GET /api/agents/{agent}/threads/{session_id}` — replay history.
- `POST /api/agents/{agent}/threads` — rotate the default thread (the "+" button).
- `POST /api/agents/{agent}/messages` — submit a message; returns `reply_topic`.
- `GET /api/streams/{reply_topic}` — Server-Sent Events stream of `Event`s.

The web client uses `EventSource` to subscribe to the SSE stream. SSE was chosen over WebSockets because the conversation is half-duplex (client posts, server streams a reply) and SSE auto-reconnects, has no framing concerns, and works through every proxy.

### 3.11 Transcript reader

The `claude` CLI writes one JSONL file per session under `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`. The encoding replaces `/` and `.` in the absolute cwd with `-`. Each agent's cwd is its own `~/.matrix/agents/<name>/work/` dir, so agents get their own non-overlapping encoded project keys.

[matrix/transcripts/reader.py](matrix/transcripts/reader.py) reads those files for the thread list and replay endpoints. It filters by `entrypoint == "sdk-py"` so interactive `claude` runs or IDE sessions in the same cwd don't show up in Matrix's UI.

Matrix never *writes* transcripts — the CLI is the sole writer.

## 4. The lifecycle of a message

End-to-end, in numbered steps:

1. User types in the web UI and hits send. JS posts `{content, session_id?}` to `POST /api/agents/assistant/messages`.
2. The web channel mints a `reply_topic` UUID, calls `session_manager.register(reply_topic)` (so events have a place to buffer), constructs an `Envelope`, and calls `harness.submit(envelope)`. Returns `{reply_topic, user_id}`.
3. The harness lazily ensures a worker exists for `(envelope.agent, envelope.user_id)` and puts the envelope on its inbox.
4. The web client opens an `EventSource` on `GET /api/streams/{reply_topic}`. The SSE handler subscribes to the topic; if the worker has already published anything, those events drain from the backlog first.
5. The worker pops the envelope. It resolves `session_id` (the envelope's, or the default thread), checks whether the on-disk transcript exists to decide `is_new`, and calls `provider.run_turn(...)`.
6. `ClaudeCodeProvider.run_turn` builds `ClaudeAgentOptions`, instantiates a `ClaudeSDKClient` with our `cli_path`, connects (which spawns a `claude` subprocess), sends the user's `query()`, and iterates `receive_response()`. Each SDK chunk is translated into Matrix `Event`s and yielded.
7. The worker awaits each yielded `Event` and publishes it to `reply_topic` via `session_manager.publish`.
8. The web SSE handler receives each event and writes `data: <json>\n\n` to the response stream. The client renders it.
9. When the SDK stream ends, the provider yields `MESSAGE_END` and disconnects the SDK client. The worker calls `session_manager.close(reply_topic)`, which sends a sentinel to all subscribers. The SSE handler exits, the EventSource closes.
10. The client refreshes the threads list (the title and message count may have updated) and re-enables the input.

If at any point the SDK errors, the provider yields an `ERROR` event (with `kind` and `message`), still yields `MESSAGE_END`, and disconnects. The worker still calls `close()`. The UI shows a red error block but the conversation can continue on the next turn — the next call gets a fresh `ClaudeSDKClient`.

## 5. Design rationales

These are the decisions that shaped the design. Each is small in isolation but compounding when violated.

### Why `claude-agent-sdk`, not LiteLLM or a custom abstraction

The constraint is OAuth subscription auth. LiteLLM, OpenRouter, and similar abstractions assume API keys, so they don't help. The Claude Pro/Max subscription is exposed via the locally-authenticated `claude` CLI; the `claude-agent-sdk` is the official ergonomic wrapper around it. For Anthropic, this is the right tool. For OpenAI, the same pattern applies — a future `CodexProvider` will wrap the `codex` CLI that ships with ChatGPT Plus/Pro.

The seam is the `Provider` interface, not a model-routing library.

### Why per-`(agent, user_id)` queues, not per-agent or per-session

Per-agent queues serialize different users behind each other — bad. Per-session queues spawn unbounded threads in conversations and lose the cross-channel continuity story (email and web in different sessions can't easily share state). Per-`(agent, user_id)` is the sweet spot: one user's conversation with one agent is consistent regardless of channel, but different users run in parallel.

The envelope still carries `session_id` so we can split a user's conversation into multiple threads when the UX wants that — see the "+" button.

### Why one process

For one user, asyncio in a single process is more than enough. A multi-process or queue-broker architecture would require process supervision, IPC contracts, deployment manifests — none of which earn their keep. If concurrency ever becomes a real bottleneck, the natural next step is per-agent processes, not a service mesh.

### Why files over a database

State is small and naturally hierarchical: one transcript file per session, one `threads.json` per agent. Files are inspectable with `cat`, version-controllable, easy to back up. SQLite enters when we need cross-agent queries (cron history, channel routing rules, audit logs).

### Why connect-on-message, disconnect-on-turn-end

Each `claude` subprocess is heavy (CLI startup ≈ 1–2s on a warm cache). For interactive web chats with one user, that latency is acceptable and avoids needing a client lifecycle pool. The clean upgrade path is an LRU cache: keep the N most recently active clients warm and disconnect older ones after T seconds idle. Phase 1 doesn't need it.

### Why declarative `agent.yaml`

Adding an agent should be a directory and a config, not a code change. This is also what makes the "stock analyst" or "coding agent" addable later without growing the harness.

### Why SSE over WebSockets

The conversation is half-duplex. SSE is HTTP, auto-reconnects, has no framing concerns, and is dead simple to consume from `EventSource`. WebSockets would only earn their keep if the client streamed back to the server inside a turn — we don't.

### Why the channel mints `reply_topic`, not the harness

The channel knows what to do with a topic — render to a browser tab, post to a Telegram chat, write to an SMTP queue. The harness doesn't care. Letting the channel mint and own the topic lets each channel implement its own routing semantics (broadcast to all open tabs, attach to a specific email thread, etc.) without the harness leaking channel concerns.

### Why we don't relocate transcripts

The `claude-agent-sdk` writes to `~/.claude/projects/<encoded-cwd>/...` and we don't fight that. Each agent's cwd is its own `~/.matrix/agents/<name>/work/`, so transcripts auto-segregate per agent. We read from there directly. The cost is one filter (`entrypoint == "sdk-py"`); the benefit is no duplicate state and full compatibility with anything else inspecting the same directory.

### Why Matrix owns context, not the CLI

The `claude` CLI has five separate auto-injection paths, each with its own switch:

1. **Auto-memory** — walks up cwd to find the nearest `MEMORY.md` in `~/.claude/projects/<encoded>/memory/`.
2. **CLAUDE.md auto-discovery** — walks up cwd to find the nearest `CLAUDE.md`.
3. **Plugin-marketplace skills** — emits a `skill_listing` attachment with descriptions of every installed plugin-skill (~2k tokens), and exposes the `Skill` tool to invoke them.
4. **Deferred tool registry** — loads the full built-in tool catalog (`TodoWrite`, `EnterPlanMode`, `Monitor`, `CronCreate`, `ToolSearch`, ...) so the agent can lazy-load any of their schemas.
5. **Auto-discovered MCP servers** — loads `~/.claude.json` connectors (claude.ai-linked Gmail/Drive/Calendar/...).

Each runs regardless of `--system-prompt` and regardless of `setting_sources=[]` — they're separate channels. Without explicit clamps, every agent in this repo inherits all five: the matrix-repo `MEMORY.md`, the developer's `CLAUDE.md`, the developer's plugin skills, the dev-mode tool catalog, and the user's claude.ai connectors. That's a lot of latent context, all of it about the *operator* of Matrix rather than the *user* the agent is serving.

Two structural defenses, then four per-agent toggles plus one architectural invariant:

- **Structural** — out-of-repo cwd at `~/.matrix/agents/<name>/work/`, so walk-up never crosses the matrix repo.
- **Per-agent toggles** — `claude_code:` block in `agent.yaml` (see [AGENTS.md §4.9](AGENTS.md)). All four default to off (`load_auto_memory`, `load_claude_mds`, `load_skills`, `load_deferred_tools`); each agent opts back in to what it needs.
- **Architectural invariant** — `--strict-mcp-config` is hardcoded in the provider, not toggleable. Matrix is the only thing that adds MCP servers. Auto-discovered ones never reach an agent.

Either the structural fence or the explicit clamps would mostly work. Both is the contract: every piece of context entering an agent's prompt was either declared in `agent.yaml` or written by Matrix code. When transcript-driven memory ships in a future phase, it'll be loaded by Matrix and passed in as part of the system prompt — never re-enabled via CLI auto-injection.

The flag-based design also gives a clean opt-in path for agents that *do* want CLI context. A coding agent will likely want `load_skills: true` (for `init`/`review`/`security-review`) and `load_deferred_tools: true` (for `TodoWrite`/`Monitor`). It declares those in its yaml; the assistant doesn't.

## 6. Conventions and gotchas

- **`user_id` always carries.** Even with one user today, the schema end-to-end has `user_id="default"`. Don't shortcut; future channels need it.
- **System messages have `user_id`s too.** Cron-triggered runs use the agent's `owner` (default `"default"`). Sub-agent calls inherit the parent's `user_id`.
- **`bypassPermissions` is fine for the assistant; revisit for the coding agent.** The coding agent will likely want `acceptEdits` or `plan` mode by default with explicit elevation.
- **Don't call the provider's CLI directly.** Always go through the SDK so we get the same options/lifecycle handling everywhere.
- **`threads.json` is mutated by the harness only.** Don't hand-edit it while Matrix is running; you'll race the worker.
- **The SDK rejects unknown `cli_path`s loudly.** If `claude` isn't on PATH, the provider fails at construction time with a clear error. Don't catch that — it's an operator problem.

## 7. Where to read next

- [README.md](README.md) — quickstart and high-level overview.
- [AGENTS.md](AGENTS.md) — the contract followed by anyone (human or AI) extending this codebase.
- [PHASE1_PLAN.md](PHASE1_PLAN.md) — the original plan; some of it is now implemented, some is still ahead.
- The code itself is small and reads in dependency order: `core/envelope.py` → `core/inbox.py` → `core/session_manager.py` → `core/threads.py` → `core/registry.py` → `providers/claude_code.py` → `core/agent.py` → `core/harness.py` → `channels/web.py` → `__main__.py`.
