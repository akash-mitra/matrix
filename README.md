# Matrix

A personal agent harness. Matrix runs one or more long-lived agents (a personal assistant today; a stock analyst, social-media manager, coding agent, and orchestrator over time), reachable through pluggable channels (web today; Telegram, email, API later) and backed by pluggable model providers (Claude Code today; OpenAI Codex, Bedrock, local models later).

It is deliberately small. The whole thing is one Python process, files-on-disk for state, and a couple of hundred lines of glue around the [`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python). It is built for one user (me), not as a product.

## Status

Phase 1 is working: a single Claude agent (`assistant`) reachable from a local web UI at `http://127.0.0.1:8765`, with streaming responses, per-agent transcript history, session resume, a thread sidebar, and explicit "new chat" support.

Phase 1 is intentionally narrow. Multi-provider, multi-channel, cron, MCP servers, memory tools, compaction, sub-agent orchestration, and the coding agent are all deferred — but the abstractions are sized to absorb them without refactoring. See [Concepts.md](Concepts.md) for the architecture and [AGENTS.md](AGENTS.md) for the contract followed when extending it.

## Quickstart

Prerequisites:

- macOS (only platform tested)
- Python 3.12 and [`uv`](https://github.com/astral-sh/uv)
- The `claude` CLI on `PATH`, authenticated against your Anthropic Pro/Max subscription (`claude auth login`)

```sh
git clone <this-repo> matrix
cd matrix
uv sync
uv run matrix          # listens on http://127.0.0.1:8765
```

Open the URL, pick the `assistant` agent in the sidebar, and start chatting. Past conversations appear in the left rail; click any to replay or continue. The `+` button creates a fresh thread.

## How it's wired

```
┌─────────────────────────── Matrix process ───────────────────────────┐
│                                                                       │
│   Channels             Harness                  Providers             │
│   ─────────            ───────                  ─────────             │
│                                                                       │
│   Web (FastAPI+SSE) ──▶  submit(envelope)                             │
│                          │                                            │
│                          ▼                                            │
│                       inboxes[(agent, user_id)]                       │
│                          │                                            │
│                          ▼                                            │
│   AgentWorker (1 per (agent, user_id))                                │
│      │                                                                │
│      ▼                                                                │
│   provider.run_turn(...) ──────▶ ClaudeCodeProvider                   │
│      │                              │                                 │
│      │                              ▼                                 │
│      │                       claude-agent-sdk ──▶ `claude` subprocess │
│      ▼                                              │                 │
│   SessionManager.publish(reply_topic, event)        │                 │
│      ▲                                              ▼                 │
│      │                                       ~/.claude/projects/      │
│   SSE subscriber (web client)                 (transcript JSONL)      │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

Channels submit `Envelope`s to the harness. Each `(agent, user_id)` pair has its own `asyncio.Queue` and a long-lived worker draining it. The worker runs a turn through the provider, which translates the SDK's message stream into Matrix `Event`s. Events are published on a per-message `reply_topic` that the channel subscribes to. Transcripts live where the `claude` CLI puts them — `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl` — and Matrix reads them for the thread sidebar.

For the design rationale and a step-by-step walk through every piece, see [Concepts.md](Concepts.md).

## Repository layout

```
matrix/
  matrix/                 # the package
    core/                 # envelope, inbox, session_manager, threads, agent, registry, harness
    providers/            # base + claude_code
    channels/             # base + web (FastAPI app)
    transcripts/          # reader for ~/.claude/projects JSONL
    web/static/           # index.html, app.js, styles.css

  agents/
    assistant/
      agent.yaml          # declarative config
      prompts/system.md
      work/               # cwd for the SDK; transcripts encode from this
      threads.json        # default thread per user_id (auto-managed)

  shared/                 # cross-agent tools / skills (empty in phase 1)

  PHASE1_PLAN.md          # living plan
  Concepts.md             # architecture and design rationale
  AGENTS.md               # contract for AI agents reading this repo
```

## Assumptions

- **Single user, local-only.** Matrix binds `127.0.0.1`. There is no auth and the threat model assumes an attacker on the loopback interface is already inside the trust boundary.
- **OAuth subscription, not API keys.** The Claude provider piggybacks on the `claude` CLI's local OAuth — no API key is required or used. Future providers (OpenAI Codex, etc.) will follow the same pattern of wrapping a vendor-supplied headless CLI.
- **Files over a database.** Transcripts are JSONL on disk (the CLI writes them, Matrix reads them). Default-thread state is one tiny `threads.json` per agent. There is no SQLite or Redis.
- **One process, asyncio everywhere.** No worker pools, no IPC, no message bus. Each agent is a long-lived `asyncio.Task` in the same process as the FastAPI app.
- **Strict serialization per `(agent, user_id)`.** Two messages from the same user to the same agent are processed in arrival order, one at a time. Different users to the same agent run in parallel (separate workers).

## What's deferred

In rough order of likely arrival:

1. **Transcript-driven memory** — surfacing things you've told an agent in past threads.
2. **Coding agent.** Its own provider configuration, with `work/` becoming a workspace for `git clone` and worktrees, and tool restrictions tuned for code review/editing.
3. **Cron-triggered runs.** A scheduler in the harness that emits `Envelope`s with `source_channel="cron"`. Implies durable inbox.
4. **More channels.** Telegram bot, email (IMAP poll + SMTP send), an HTTP API for programmatic use.
5. **Orchestrator agent + sub-agent-as-tool.** A custom tool exposed to the orchestrator's provider that submits to a sibling agent's inbox and awaits the reply.
6. **More providers.** OpenAI Codex (subscription OAuth), Bedrock (API), local (Ollama / llama.cpp).
7. **MCP servers per agent.** Listed in `agent.yaml`, wired through the SDK's MCP support.
8. **Idle-disconnect LRU for SDK clients.** Today: connect-on-message / disconnect-on-turn-end. When concurrency grows, hold N clients warm and disconnect the rest.
9. **Multi-thread per `(agent, user_id)`.** Today there is one default thread per user; the envelope schema already carries an explicit `session_id` so the harness can fan a single user's conversations out into multiple parallel threads when the UX needs it.
10. **Permission/security model.** Today every tool is allowlisted globally per agent and `permission_mode="bypassPermissions"`. Future: per-tool per-channel constraints, especially for the coding agent.

## License

Personal project. No license, no warranty, not for commercial use.
