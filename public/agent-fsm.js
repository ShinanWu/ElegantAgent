/**
 * 每个 Agent 一个状态机。
 *
 * 阶段 (phase):
 *   idle    — 无进行中的 run
 *   running — SDK run 进行中，可接收 stream 事件
 *
 * 持久数据: messages, messageCount, meta
 * 运行期数据 (running): stream
 * 视图引用 (仅当前挂载的 DOM): view
 */
const AgentFSM = (() => {
  const Phase = Object.freeze({
    IDLE: "idle",
    RUNNING: "running",
  });

  /** @type {Map<string, object>} */
  const agents = new Map();

  function emptyStream() {
    return {
      text: "",
      segments: [],
      thinkingText: "",
      toolBlocks: [],
      activityText: "Agent 运行中…",
    };
  }

  function syncStreamText(stream) {
    stream.text = (stream.segments || [])
      .filter((seg) => seg.type === "text")
      .map((seg) => seg.text || "")
      .join("");
  }

  function applyPayloadToSegments(segments, payload) {
    if (payload.text) {
      const chunk = String(payload.text);
      if (segments.length && segments[segments.length - 1].type === "text") {
        segments[segments.length - 1].text += chunk;
      } else {
        segments.push({ type: "text", text: chunk });
      }
    }
    const blocks = [];
    if (payload.block) blocks.push(payload.block);
    if (payload.blocks) blocks.push(...payload.blocks);
    for (const block of blocks) {
      if (!block || !block.type) continue;
      if (block.type === "thinking") {
        const text = block.text || "";
        if (segments.length && segments[segments.length - 1].type === "thinking") {
          segments[segments.length - 1].text += text;
        } else {
          segments.push({ type: "thinking", text });
        }
        continue;
      }
      if (block.type === "tool") {
        const key = block.id || block.name || "tool";
        let updated = false;
        for (let i = segments.length - 1; i >= 0; i -= 1) {
          const seg = segments[i];
          if (seg.type !== "tool") continue;
          const segKey = seg.id || seg.name || "tool";
          if (segKey === key) {
            Object.assign(seg, block, { type: "tool" });
            updated = true;
            break;
          }
        }
        if (!updated) segments.push({ ...block, type: "tool" });
      }
    }
  }

  function applyStreamPayload(stream, msg) {
    if (!stream.segments) stream.segments = [];
    if (msg.activity && !isTerminalActivity(msg.activity)) {
      stream.activityText = msg.activity;
    }
    applyPayloadToSegments(stream.segments, msg);
    syncStreamText(stream);
    stream.thinkingText = stream.segments
      .filter((seg) => seg.type === "thinking")
      .map((seg) => seg.text || "")
      .join("");
    stream.toolBlocks = stream.segments.filter((seg) => seg.type === "tool");
    const runningTool = stream.toolBlocks.find((block) => block.status === "running");
    if (runningTool) {
      stream.activityText = `运行中 · ${runningTool.label || runningTool.name}`;
    }
  }

  function emptyView() {
    return { streamingEl: null, thinkingEl: null };
  }

  function create(agentId, init = {}) {
    const agent = {
      id: agentId,
      phase: init.running ? Phase.RUNNING : Phase.IDLE,
      messages: Array.isArray(init.messages) ? init.messages.slice() : [],
      messageCount: init.messageCount ?? 0,
      meta: { ...(init.meta || {}) },
      stream: emptyStream(),
      view: emptyView(),
    };
    agents.set(agentId, agent);
    return agent;
  }

  function ensure(agentId) {
    if (!agentId) return null;
    if (!agents.has(agentId)) create(agentId);
    return agents.get(agentId);
  }

  function get(agentId) {
    return agents.get(agentId) || null;
  }

  function remove(agentId) {
    agents.delete(agentId);
  }

  function detachView(agentId) {
    const agent = get(agentId);
    if (!agent) return;
    agent.view = emptyView();
  }

  function isRunning(agentId) {
    const agent = get(agentId);
    return agent?.phase === Phase.RUNNING;
  }

  function messages(agentId) {
    return get(agentId)?.messages || [];
  }

  function clearStream(agent) {
    agent.stream = emptyStream();
  }

  function messagesEquivalent(a, b) {
    if (!a || !b) return false;
    if (a.role !== b.role) return false;
    if (a.segments?.length || b.segments?.length) {
      return JSON.stringify(a.segments || []) === JSON.stringify(b.segments || []);
    }
    if (String(a.content || "").trim() !== String(b.content || "").trim()) return false;
    return JSON.stringify(a.blocks || []) === JSON.stringify(b.blocks || []);
  }

  function applyMeta(agent, detail) {
    agent.meta = {
      name: detail.name ?? agent.meta.name ?? "",
      cwd: detail.cwd ?? agent.meta.cwd ?? "",
      model: detail.model ?? agent.meta.model ?? "",
      enableSoul: !!detail.enableSoul,
      enableRules: !!detail.enableRules,
      enableSkills: !!detail.enableSkills,
      enableMemory: !!detail.enableMemory,
      rulesDir: detail.rulesDir ?? agent.meta.rulesDir ?? "",
      skillsDir: detail.skillsDir ?? agent.meta.skillsDir ?? "",
      memoryDir: detail.memoryDir ?? agent.meta.memoryDir ?? "",
    };
    if (detail.messageCount != null) agent.messageCount = detail.messageCount;
  }

  function dispatch(agentId, event, payload = {}) {
    const agent = ensure(agentId);
    const prevPhase = agent.phase;
    const result = {
      agent,
      event,
      prevPhase,
      phaseChanged: false,
      messagesChanged: false,
      streamChanged: false,
      needsResync: false,
      committed: null,
    };

    switch (event) {
      case "created":
        applyMeta(agent, payload);
        agent.messages = [];
        agent.messageCount = 0;
        agent.phase = Phase.IDLE;
        clearStream(agent);
        break;

      case "snapshot": {
        const incoming = payload.messages || [];
        const local = agent.messages;
        applyMeta(agent, payload);
        if (payload.running) {
          agent.phase = Phase.RUNNING;
        } else if (agent.phase === Phase.RUNNING && !payload.running) {
          agent.phase = Phase.IDLE;
          clearStream(agent);
          result.streamChanged = true;
        }
        if (incoming.length < local.length) {
          result.needsResync = false;
          break;
        }
        const same =
          incoming.length === local.length &&
          (local.length === 0 || local.every((m, i) => messagesEquivalent(m, incoming[i])));
        if (!same) {
          agent.messages = incoming.slice();
          result.messagesChanged = true;
        }
        break;
      }

      case "meta_updated":
        applyMeta(agent, payload);
        if (payload.running != null) {
          syncServerRunning(agent, !!payload.running, result);
        }
        break;

      case "server_running":
        syncServerRunning(agent, !!payload.running, result);
        break;

      case "message_committed": {
        const { message, index, messageCount } = payload;
        if (messageCount != null) agent.messageCount = messageCount;
        if (index < agent.messages.length) {
          if (messagesEquivalent(agent.messages[index], message)) break;
          result.needsResync = true;
          break;
        }
        if (index > agent.messages.length) {
          result.needsResync = true;
          break;
        }
        agent.messages.push(message);
        result.messagesChanged = true;
        result.committed = message;
        break;
      }

      case "run_started":
        agent.phase = Phase.RUNNING;
        clearStream(agent);
        result.streamChanged = true;
        break;

      case "stream": {
        if (agent.phase !== Phase.RUNNING) break;
        applyStreamDelta(agent, payload);
        result.streamChanged = true;
        break;
      }

      case "run_finished":
      case "run_cancelled":
        if (payload.messageCount != null) agent.messageCount = payload.messageCount;
        agent.phase = Phase.IDLE;
        clearStream(agent);
        result.streamChanged = true;
        break;

      case "run_error":
        agent.phase = Phase.IDLE;
        clearStream(agent);
        result.streamChanged = true;
        result.needsResync = true;
        break;

      case "ws_disconnected":
        agent.phase = Phase.IDLE;
        clearStream(agent);
        result.streamChanged = true;
        break;

      default:
        break;
    }

    result.phaseChanged = prevPhase !== agent.phase;
    return result;
  }

  function syncServerRunning(agent, running, result) {
    if (running && agent.phase === Phase.IDLE) {
      agent.phase = Phase.RUNNING;
    } else if (!running && agent.phase === Phase.RUNNING) {
      agent.phase = Phase.IDLE;
      clearStream(agent);
      result.streamChanged = true;
    }
  }

  function isTerminalActivity(text) {
    return ["finished", "completed", "done", "cancelled", "canceled", "error", "failed"].includes(
      String(text || "").trim().toLowerCase()
    );
  }

  function applyStreamDelta(agent, msg) {
    applyStreamPayload(agent.stream, msg);
  }

  function recordStreamBlock(stream, block) {
    applyStreamPayload(stream, { block });
  }

  function finalizeStreamView(agent) {
    if (agent.view.thinkingEl) agent.view.thinkingEl.open = false;
    if (agent.view.streamingEl?.parentNode) agent.view.streamingEl.remove();
    agent.view.streamingEl = null;
    agent.view.thinkingEl = null;
  }

  function removeStreamViewBeforeCommit(agent) {
    if (agent.view.streamingEl?.parentNode) agent.view.streamingEl.remove();
    agent.view.streamingEl = null;
    agent.view.thinkingEl = null;
  }

  return {
    Phase,
    create,
    ensure,
    get,
    remove,
    detachView,
    dispatch,
    isRunning,
    messages,
    finalizeStreamView,
    removeStreamViewBeforeCommit,
    messagesEquivalent,
    applyStreamPayload,
  };
})();
