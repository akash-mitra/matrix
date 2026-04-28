# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor, etc.) working on Matrix. This file complements [README.md](README.md) and [Concepts.md](Concepts.md). Read **Concepts.md first** if you haven't — most decisions you'd be tempted to make differently are explained there.

The orientation here is: what Matrix *is*, what's deliberately *not* in scope, the contract you must respect when extending it, and the failure modes that look like bugs but are actually load-bearing design.

## 1. What Matrix is (and isn't)

**Is:** A personal agent harness for one user (the repo owner), running locally on macOS. One Python process, asyncio everywhere, files-on-disk for state, OAuth subscription auth via vendor-supplied headless CLIs. Phase 1 ships a single Claude agent reachable from a local web UI. See [README.md](README.md#status) for the current state.

**Isn't:**

- A product. There is no multi-tenancy, no auth, no rate-limiting, no observability stack. Don't add them.
- A general LLM router. There is no LiteLLM, no model fallback, no API-key handling. Don't add them.
- A serving framework. We are not building Vercel for agents. Don't introduce queues (Redis, Kafka), workers (Celery), or schedulers (Airflow). When durability is needed, the next step is SQLite or a directory of JSON files, not a broker.
- A reimplementation of Claude Code. We *use* `claude-agent-sdk`. Don't rewrite the subprocess management.

When a request implies any of the above, push back. Most of the time the user has a smaller, more specific need, and the right answer is a 30-line addition, not a framework.

## 2. Architecture in one breath

`Channel` produces an `Envelope` → `Harness.submit()` → inbox keyed by `(agent, user_id)` → `AgentWorker` (one per pair, long-lived) drains it → `Provider.run_turn()` translates the backend's stream into Matrix `Event`s → `SessionManager.publish(reply_topic, event)` → channel-side subscriber renders.

Read [Concepts.md](Concepts.md#3-the-pieces-bottom-up) for the full walk. If anything in your change crosses two of those layers, stop and re-read — you probably want a smaller change.

## 3. Where things live

```
matrix/
  core/envelope.py        # Envelope, Event, EventType — start here
  core/inbox.py           # Inbox protocol + InMemoryInbox
  core/session_manager.py # pub/sub for reply topics, with backlog buffering
  core/threads.py         # default-thread map per agent (threads.json)
  core/registry.py        # loads agents/<name>/agent.yaml into AgentConfig
  core/agent.py           # AgentWorker — long-lived asyncio task per (agent, user_id)
  core/harness.py         # owns it all; harness.submit(envelope) is the entry point
  providers/base.py       # Provider Protocol
  providers/claude_code.py# wraps claude-agent-sdk's ClaudeSDKClient
  channels/base.py        # Channel Protocol
  channels/web.py         # FastAPI app + SSE + thread routes
  transcripts/reader.py   # parses ~/.claude/projects/<encoded-cwd>/<id>.jsonl
  web/static/             # index.html, app.js, styles.css — vanilla JS, no build
  __main__.py             # `uv run matrix` entrypoint

agents/<name>/
  agent.yaml              # declarative config (provider, model, prompt, tools, ...)
  prompts/system.md       # system prompt for the agent
  work/                   # cwd handed to the provider; transcripts encode from this
  threads.json            # auto-managed; do not hand-edit while Matrix is running
```

## 4. The contract

These are the invariants. Don't violate them without a written reason and approval from the user.

### 4.1 The Envelope is the only thing that crosses the channel/harness boundary

Channels do not call providers. Channels do not read transcripts directly (they go through harness/transcripts module). Providers do not see HTTP requests. If you find yourself plumbing a FastAPI `Request` into a worker or an `AssistantMessage` into a channel, you have crossed a layer.

### 4.2 `(agent, user_id)` is the queue key

Not `(agent,)`, not `(session_id,)`. Different users run in parallel; one user's messages to one agent are strictly serial. This is what gives us cross-channel conversation continuity and prevents two users from blocking each other.

### 4.3 `session_id` may be present without an existing transcript

When the UI rotates the default thread, it pins a fresh UUID *before* any message has been sent on it. The worker detects "session_id supplied but no JSONL on disk" and treats it as a new session for the SDK (`session_id=`, not `resume=`). See [matrix/core/agent.py](matrix/core/agent.py). Do not "simplify" this check away.

### 4.4 OAuth subscription, not API keys

Matrix never reads `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc. The provider relies on the vendor's locally-authenticated CLI. If you're tempted to add API-key support "for development," stop — that's a different product. Use `claude auth login`.

### 4.5 Don't relocate transcripts

The `claude-agent-sdk` writes JSONL to `~/.claude/projects/<encoded-cwd>/<id>.jsonl`. Matrix reads from there. We don't move them, we don't proxy them, we don't symlink them. If you want a per-agent view, use the fact that each agent's `cwd` is its own `work/` directory — encoding handles the rest.

### 4.6 The SessionManager backlog is load-bearing

The web client POSTs to submit a message and *then* opens an SSE connection — there is a real window where the worker has already started publishing. The `SessionManager.register(topic)` call before submission, and the backlog drained by the first subscriber, exist to make this race impossible. Do not remove the registration step or the backlog.

### 4.7 Provider events are the lingua franca

The web UI knows about `message.start | message.delta | tool.use | tool.result | thinking | message.end | error`. That is the contract. New providers translate their backend's stream into these. New channels consume these. Do not invent new event types unless you also update both ends with a clear reason.

### 4.8 `agent.yaml` is forward-compatible

The parser ignores unknown keys today. The names `mcp_servers`, `cron`, `channels`, `sub_agents` are reserved. If you add a key that overlaps with one of these reserved names, make sure the semantics line up with the future plans in [README.md](README.md#whats-deferred).

## 5. Common tasks (and how to do them)

### Adding a new agent

```sh
mkdir -p agents/<name>/{prompts,work,tools,skills}
$EDITOR agents/<name>/agent.yaml      # see agents/assistant/agent.yaml
$EDITOR agents/<name>/prompts/system.md
```

No Python changes if the provider already exists. Restart Matrix; the registry picks it up.

### Adding a new provider

1. Implement the `Provider` Protocol in `matrix/providers/<name>.py`. Translate the backend's stream into Matrix `Event`s — match the existing claude_code translator.
2. Wire it into `Harness._build_provider` in `matrix/core/harness.py`.
3. Reference it in an `agent.yaml` via `provider: <name>`.

For OAuth-subscription providers (Codex), wrap the vendor's headless CLI the same way `ClaudeCodeProvider` wraps the `claude` CLI. For API-keyed providers (Bedrock), read the key from a per-provider config — *not* a generic `API_KEY` env var.

### Adding a new channel

1. Implement the `Channel` Protocol. Produce `Envelope`s with a stable `user_id` (the channel is responsible for identifying the sender — cookie, Telegram chat ID, verified email From, etc.). Mint your own `reply_topic`.
2. Subscribe to the topic to deliver replies. Translate Matrix `Event`s into your channel's idiom.
3. Wire it into the harness boot sequence in `matrix/__main__.py`.

A channel that can't produce a trusted `user_id` should refuse the message rather than fall back to `default`.

### Adding a new tool

Phase 1 only uses tools built into the `claude` CLI (`Read`, `Write`, `Bash`, etc.). Per-agent tool allowlists are in `agent.yaml` under `allowed_tools`. To add a custom Python-level tool that the SDK can invoke, you would use the SDK's tool registration — that path is not yet exercised in Matrix. Coordinate with the user before introducing it; it likely belongs in `shared/tools/` or `agents/<name>/tools/`.

## 6. Things that look like bugs but aren't

- **Each turn spawns a fresh `claude` subprocess.** Phase 1 design — connect-on-message, disconnect-on-turn-end. The LRU client cache is deferred. See [Concepts.md §5](Concepts.md#5-design-rationales).
- **The same agent's two simultaneous web tabs serialize their messages.** The `(agent, user_id)` queue is single-consumer. Two browser tabs as the same user are *the same user*; they share the queue.
- **`threads.json` shows a session_id with no transcript file.** The UI just rotated the default; nothing has been sent yet. The next message will create the transcript.
- **The UI's thread sidebar takes a moment to update after sending.** It refreshes on `message.end` — by design, since the SDK auto-titler may update the title at the end.
- **A non-Matrix `claude` invocation in the same project root pollutes the cwd's transcripts.** It does, but the reader filters by `entrypoint == "sdk-py"` so the UI ignores them. The files still exist on disk; that's a `claude` CLI fact, not a Matrix problem.
- **Killing Matrix mid-turn leaves a `claude` subprocess for a moment.** SDK disconnect on cleanup; usually the OS reaps quickly. If you see lingering subprocesses, that's a real bug.

## 7. Future work — what's on deck

These are *deferred*, not vetoed. If your change is in service of one of these and the user has asked, fine. If you're tempted to do one preemptively, ask first.

1. Transcript-driven memory.
2. Coding agent (Claude Code only; uses git worktrees in `agents/coding/work/<repo>/`).
3. Cron-triggered runs (durable inbox required).
4. Telegram, email, and HTTP API channels.
5. Orchestrator + sub-agent-as-tool.
6. Codex provider (OAuth, mirrors ClaudeCodeProvider), Bedrock, local.
7. MCP servers per agent.
8. Idle-disconnect LRU for SDK clients.
9. Multi-thread per `(agent, user_id)`.
10. Permission model per-tool per-channel.

See [README.md §What's deferred](README.md#whats-deferred) for the canonical list with brief context.

## 8. Code review checklist

When reviewing a change to Matrix, ask:

- Does it cross a layer it shouldn't? (channel calling provider, provider parsing HTTP, etc.)
- Does it preserve the `(agent, user_id)` queue contract?
- Does it keep `Envelope` and `Event` as the only types crossing layer boundaries?
- Does it touch `claude-agent-sdk` in a way that contradicts the connect-on-message lifecycle?
- Does it add API-key handling, broker dependencies, or auth that aren't justified?
- Does it preserve forward-compatibility of `agent.yaml` (no breaking renames)?
- Does it avoid pre-building infrastructure for deferred features?
- Are tests realistic (no mocking the inbox out of existence)?
- Are comments explaining *why*, not *what*?

If the answer to any of those is "no," the change probably needs to be smaller, or the rationale needs to be in the PR description.

## 9. Style

- **Python:** 3.12+, async-first, type-annotated. `from __future__ import annotations` at the top of every module. Dataclasses for value types. Protocols for interfaces. No external linters configured yet, so match the surrounding style.
- **No comments that restate code.** Comments explain *why*, especially when the why is non-obvious — load-bearing constraints, race conditions, references to external behavior. See [Concepts.md §6](Concepts.md#6-conventions-and-gotchas) for examples.
- **No new top-level docs** without an explicit ask. Update existing docs in place. The triad is README.md + Concepts.md + AGENTS.md; resist the urge to add a fourth.
- **Web client:** vanilla JS, no build step, no framework. Keep it that way.

## 10. Asking the user

Reach for the user when:

- The change implies a deferred feature being pulled forward.
- A constraint here conflicts with what you'd normally do (e.g. "add API key support").
- A design decision was *implicit* in this doc and you're about to make a different one.

Don't ask before small, in-scope work. Do ask before architectural moves.
