const agentSelect = document.getElementById("agent-select");
const newChatBtn = document.getElementById("new-chat");
const threadsList = document.getElementById("threads");
const messages = document.getElementById("messages");
const threadTitle = document.getElementById("thread-title");
const composer = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = composer.querySelector("button");

const state = {
  agent: null,
  sessionId: null,
};

function fmtTime(iso) {
  const d = new Date(iso);
  const now = new Date();
  const sameDay =
    d.toDateString() === now.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function clearMessages() {
  messages.innerHTML = "";
}

function appendMsg(cls, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.textContent = text;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

function renderHistoryItem(item) {
  if (item.role === "user") {
    for (const b of item.blocks) {
      if (b.type === "text") appendMsg("user", b.text);
      else if (b.type === "tool_result") {
        const c = b.content;
        const text = typeof c === "string" ? c : JSON.stringify(c);
        appendMsg("tool", `← ${text}`);
      }
    }
  } else if (item.role === "assistant") {
    for (const b of item.blocks) {
      if (b.type === "text") appendMsg("assistant", b.text);
      else if (b.type === "tool_use") {
        appendMsg(
          "tool",
          `→ ${b.name}(${JSON.stringify(b.input)})`,
        );
      }
    }
  }
}

async function loadAgents() {
  const res = await fetch("/api/agents");
  const agents = await res.json();
  agentSelect.innerHTML = "";
  for (const a of agents) {
    const opt = document.createElement("option");
    opt.value = a.name;
    opt.textContent = a.description ? `${a.name} — ${a.description}` : a.name;
    agentSelect.appendChild(opt);
  }
  state.agent = agents[0]?.name ?? null;
}

async function refreshThreads({ autoOpenDefault = false } = {}) {
  if (!state.agent) return;
  const res = await fetch(`/api/agents/${state.agent}/threads`);
  const data = await res.json();
  threadsList.innerHTML = "";
  for (const t of data.threads) {
    const li = document.createElement("li");
    li.dataset.sessionId = t.session_id;
    if (t.is_default) li.classList.add("is-default");
    if (t.session_id === state.sessionId) li.classList.add("active");
    li.innerHTML =
      `<div>${escapeHtml(t.title)}</div>` +
      `<span class="ts">${fmtTime(t.updated_at)} · ${t.message_count} msgs</span>`;
    li.addEventListener("click", () => openThread(t.session_id, t.title));
    threadsList.appendChild(li);
  }
  if (autoOpenDefault && data.default_session_id && !state.sessionId) {
    const found = data.threads.find(
      (t) => t.session_id === data.default_session_id,
    );
    if (found) await openThread(found.session_id, found.title);
  }
}

async function openThread(sessionId, title) {
  state.sessionId = sessionId;
  threadTitle.textContent = title || "Thread";
  for (const li of threadsList.querySelectorAll("li")) {
    li.classList.toggle("active", li.dataset.sessionId === sessionId);
  }
  clearMessages();
  const res = await fetch(`/api/agents/${state.agent}/threads/${sessionId}`);
  if (!res.ok) {
    appendMsg("error", `failed to load thread (${res.status})`);
    return;
  }
  const data = await res.json();
  for (const item of data.items) renderHistoryItem(item);
}

async function startNewChat() {
  const res = await fetch(`/api/agents/${state.agent}/threads`, {
    method: "POST",
  });
  if (!res.ok) {
    appendMsg("error", `failed to create thread (${res.status})`);
    return;
  }
  const { session_id } = await res.json();
  state.sessionId = session_id;
  threadTitle.textContent = "New chat";
  clearMessages();
  for (const li of threadsList.querySelectorAll("li")) {
    li.classList.remove("active");
  }
  input.focus();
}

async function send(content) {
  appendMsg("user", content);
  sendBtn.disabled = true;
  let assistantDiv = null;

  const body = { content };
  if (state.sessionId) body.session_id = state.sessionId;

  const submitRes = await fetch(`/api/agents/${state.agent}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!submitRes.ok) {
    appendMsg("error", `submit failed: ${submitRes.status}`);
    sendBtn.disabled = false;
    return;
  }
  const { reply_topic } = await submitRes.json();

  await new Promise((resolve) => {
    const es = new EventSource(`/api/streams/${reply_topic}`);
    es.onmessage = (e) => {
      const evt = JSON.parse(e.data);
      switch (evt.type) {
        case "message.start":
          if (evt.data.session_id) state.sessionId = evt.data.session_id;
          break;
        case "message.delta":
          if (!assistantDiv) assistantDiv = appendMsg("assistant", "");
          assistantDiv.textContent += evt.data.text;
          messages.scrollTop = messages.scrollHeight;
          break;
        case "tool.use":
          appendMsg(
            "tool",
            `→ ${evt.data.name}(${JSON.stringify(evt.data.input)})`,
          );
          assistantDiv = null;
          break;
        case "tool.result": {
          const c = evt.data.content;
          const text = typeof c === "string" ? c : JSON.stringify(c);
          appendMsg("tool", `← ${text}`);
          break;
        }
        case "error":
          appendMsg("error", `${evt.data.kind}: ${evt.data.message}`);
          break;
        case "message.end":
          es.close();
          resolve();
          break;
      }
    };
    es.onerror = () => {
      es.close();
      resolve();
    };
  });

  sendBtn.disabled = false;
  await refreshThreads();
  input.focus();
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

agentSelect.addEventListener("change", async () => {
  state.agent = agentSelect.value;
  state.sessionId = null;
  clearMessages();
  threadTitle.textContent = "Matrix";
  await refreshThreads({ autoOpenDefault: true });
});

newChatBtn.addEventListener("click", startNewChat);

composer.addEventListener("submit", (e) => {
  e.preventDefault();
  const content = input.value.trim();
  if (!content) return;
  input.value = "";
  send(content);
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    composer.requestSubmit();
  }
});

(async () => {
  await loadAgents();
  await refreshThreads({ autoOpenDefault: true });
  input.focus();
})();
