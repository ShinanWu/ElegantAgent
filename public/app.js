const LAST_AGENT_KEY = "yoya:last-agent";
const FONT_SIZE_KEY = "yoya:font-size";
const SCROLL_PREFIX = "yoya:scroll:";
const ATTACH_MARKER_RE = /\[\[πattach:(.*?)\]\]/g;
const YOYA_CLIPBOARD = "application/x-yoya-attachments";

function encodeAttachmentMarker(path) {
  return `[[πattach:${path}]]`;
}

const NO_INSTRUCTION_TEXT = "（未识别到意图）";
const NO_INSTRUCTION_LEGACY = new Set([
  "（无指令意图）",
  "无指令意图",
  "（无意图）",
  "（未识别到意图）",
]);

const state = {
  ws: null,
  connected: false,
  reconnectDelay: 400,
  reconnectTimer: null,
  agents: [],
  activeAgentId: null,
  activeAgent: null,
  discussions: [],
  discussionRuntimes: {},
  defaultCwd: "",
  defaultModel: "composer-2.5",
  version: "",
  models: [],
  needsSetup: false,
  shellVisible: true,
  autoScroll: true,
  suppressScrollAutoUpdate: false,
  discussRailOpen: false,
  expandedDiscussionId: null,
  maximizedDiscussionId: null,
  selectedDiscussionIds: new Set(),
  combinedSummaries: [],
  summaryRuntimes: {},
  combinedSummaryRuntimes: {},
  summarySectionOpen: new Set(),
  savingAgent: false,
  creatingAgent: false,
  engineState: "connecting",
  engineMessage: "正在连接…",
  waitHint: "",
  openDrawerAfterSelect: false,
  linkViewerUrl: "",
  contextMenuFromSelection: false,
  suppressSelectionContextMenuUntil: 0,
  ignoreContextMenuDismissUntil: 0,
  configFiles: { soul: "", rulesTree: [], skillsTree: [], memoryTree: [] },
  configEditor: { source: "", path: "", content: "", dirty: false },
};

let mainComposer = null;

const $ = (id) => document.getElementById(id);

function getActiveAgent() {
  return state.activeAgentId ? AgentFSM.get(state.activeAgentId) : null;
}

function agentSummaryRunning(agentId) {
  const listItem = state.agents.find((a) => a.id === agentId);
  return AgentFSM.isRunning(agentId) || !!listItem?.running;
}

function getDiscussionRuntime(discussionId) {
  if (!state.discussionRuntimes[discussionId]) {
    state.discussionRuntimes[discussionId] = {
      busy: false,
      streamingText: "",
      segments: [],
      thinkingText: "",
      toolBlocks: [],
    };
  }
  return state.discussionRuntimes[discussionId];
}

function msgAgentId(msg) {
  return msg.agentId || state.activeAgentId;
}

function isActive(agentId) {
  return agentId === state.activeAgentId;
}

function isActiveBusy() {
  return AgentFSM.isRunning(state.activeAgentId);
}

function send(payload) {
  if (state.ws?.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

function ensureConnected(message) {
  if (state.ws?.readyState === WebSocket.OPEN) return true;
  showToast(message || "尚未连接到 Agent 服务，请稍后再试", "error");
  return false;
}

function applyShellState(visible) {
  if (state.shellVisible === visible) return;
  state.shellVisible = visible;
  if (visible) onWindowShown();
  else onWindowHidden();
}

function onWindowHidden() {
  if (state.activeAgentId) saveMessageScrollPosition(state.activeAgentId);
}

function onWindowShown() {
  if (state.needsSetup || !state.activeAgentId) return;
  paintActiveConversation(state.activeAgentId);
  resyncActiveAgent();
}

function resyncActiveAgent() {
  if (!state.activeAgentId || !state.connected) return;
  send({ type: "list_agents" });
  fetchConversation(state.activeAgentId);
  send({ type: "list_discussions", agentId: state.activeAgentId });
  send({ type: "list_combined_summaries", agentId: state.activeAgentId });
}

function connect() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  state.engineState = "connecting";
  state.engineMessage = "";
  updateChrome();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);

  state.ws.onopen = () => {
    state.connected = true;
    state.reconnectDelay = 400;
    updateChrome();
    if (!state.needsSetup) {
      send({ type: "list_agents" });
      send({ type: "list_models" });
      if (state.activeAgentId && state.shellVisible) {
        paintActiveConversation(state.activeAgentId);
        resyncActiveAgent();
      }
    }
  };

  state.ws.onclose = () => {
    state.connected = false;
    state.ws = null;
    state.engineState = "connecting";
    state.engineMessage = "";
    for (const a of state.agents) {
      const fx = AgentFSM.dispatch(a.id, "ws_disconnected");
      if (isActive(a.id) && fx.streamChanged) AgentFSM.finalizeStreamView(fx.agent);
    }
    updateChrome();
    const delay = state.reconnectDelay;
    state.reconnectDelay = Math.min(Math.round(delay * 1.7), 8000);
    state.reconnectTimer = setTimeout(connect, delay);
  };

  state.ws.onmessage = (ev) => handleMessage(JSON.parse(ev.data));
}

function syncRuntimeFromServer() {
  for (const a of state.agents) {
    const fx = AgentFSM.dispatch(a.id, "server_running", { running: !!a.running });
    a.running = AgentFSM.isRunning(a.id);
    if (!isActive(a.id) && fx.streamChanged) AgentFSM.finalizeStreamView(fx.agent);
  }
  updateChrome();
  renderAgents();
}

function handleMessage(msg) {
  const aid = msgAgentId(msg);

  switch (msg.type) {
    case "hello":
      state.defaultCwd = msg.defaultCwd;
      state.defaultModel = msg.defaultModel;
      state.version = msg.version || "";
      state.needsSetup = !!msg.needsSetup;
      state.shellVisible = msg.shellVisible !== false;
      $("setup-cwd").value = state.defaultCwd;
      fillModelSelect($("setup-model"), state.defaultModel);
      if (state.needsSetup) {
        showSetup(true);
        return;
      }
      hideSetup();
      if (msg.engineReady) {
        state.engineState = "ready";
        state.engineMessage = "";
      } else if (state.engineState !== "ready") {
        state.engineState = "starting";
        state.engineMessage = "正在启动引擎…";
      }
      updateChrome();
      break;

    case "shell":
      applyShellState(!!msg.visible);
      break;

    case "engine_status":
      state.engineState = msg.state || state.engineState;
      state.engineMessage = msg.message || "";
      if (msg.state === "error") {
        showToast(msg.message || "引擎启动失败", "error");
      }
      updateChrome();
      break;

    case "agents":
      state.agents = msg.agents.map((a) => ({ ...a, running: !!a.running }));
      for (const a of state.agents) AgentFSM.ensure(a.id);
      syncRuntimeFromServer();
      if (!state.activeAgentId) restoreLastAgent();
      break;

    case "agent_created":
      state.creatingAgent = false;
      $("btn-new-agent").disabled = false;
      if (state.waitHint === "正在创建 Agent…") state.waitHint = "";
      state.agents.unshift({
        id: msg.agent.id,
        name: msg.agent.name,
        cwd: msg.agent.cwd,
        model: msg.agent.model,
        messageCount: 0,
        running: false,
      });
      AgentFSM.dispatch(msg.agent.id, "created", msg.agent);
      selectAgent(msg.agent.id, msg.agent);
      renderAgents();
      break;

    case "agent":
      loadConversation(msg.agent);
      break;

    case "agent_updated":
      {
        state.savingAgent = false;
        resetSaveAgentButton();
        const idx = state.agents.findIndex((a) => a.id === msg.agent.id);
        if (idx >= 0) {
          state.agents[idx] = { ...state.agents[idx], ...msg.agent, running: !!msg.agent.running };
        }
        const fx = AgentFSM.dispatch(msg.agent.id, "meta_updated", {
          ...msg.agent,
          running: !!msg.agent.running,
        });
        if (isActive(msg.agent.id)) {
          state.activeAgent = { ...msg.agent, messages: fx.agent.messages };
          $("model-name").textContent = msg.agent.model;
          populateAgentDrawer(msg.agent);
        }
        renderAgents();
        showToast("Agent 配置已保存");
        setDrawerSaveStatus("已保存");
        if (isActive(msg.agent.id)) {
          send({ type: "read_agent_files", agentId: msg.agent.id });
        }
      }
      break;

    case "agent_deleted":
      AgentFSM.remove(msg.agentId);
      state.agents = state.agents.filter((a) => a.id !== msg.agentId);
      state.discussions = state.discussions.filter((d) => d.agentId !== msg.agentId);
      state.combinedSummaries = state.combinedSummaries.filter((c) => c.agentId !== msg.agentId);
      state.selectedDiscussionIds = new Set();
      state.summarySectionOpen = new Set();
      if (state.activeAgentId === msg.agentId) {
        state.activeAgentId = null;
        state.activeAgent = null;
        if (state.agents.length) selectAgent(state.agents[0].id);
        else createAgent();
      }
      renderAgents();
      renderDiscussions();
      refreshAllDiscussionHighlights();
      if (state.waitHint === "正在删除 Agent…") state.waitHint = "";
      updateChrome();
      break;

    case "agent_reset":
      {
        AgentFSM.dispatch(msg.agent.id, "reset", msg.agent);
        const idx = state.agents.findIndex((a) => a.id === msg.agent.id);
        if (idx >= 0) {
          state.agents[idx] = { ...state.agents[idx], ...msg.agent, running: false };
        }
        if (isActive(msg.agent.id)) {
          closeAgentDrawer();
          state.activeAgent = { ...msg.agent, messages: [] };
          state.discussions = [];
          state.combinedSummaries = [];
          state.selectedDiscussionIds = new Set();
          state.summaryRuntimes = {};
          state.combinedSummaryRuntimes = {};
          state.summarySectionOpen = new Set();
          state.expandedDiscussionId = null;
          state.maximizedDiscussionId = null;
          $("model-name").textContent = msg.agent.model;
          renderMessages([], { scroll: "bottom" });
          clearComposer();
          renderDiscussions();
          renderCombinedSummaries();
          refreshAllDiscussionHighlights();
          updateChrome();
          requestAnimationFrame(() => mainComposer?.focus());
          showToast("Agent 已重置");
        }
        if (state.waitHint === "正在重置 Agent…") state.waitHint = "";
        updateChrome();
        renderAgents();
      }
      break;

    case "agent_files":
      if (isActive(msg.agentId) && msg.soul != null) {
        $("soul-editor").value = msg.soul;
      }
      if (isActive(msg.agentId)) applyAgentConfigFiles(msg);
      break;

    case "agent_file":
      if (isActive(msg.agentId)) openConfigFileEditor(msg.source, msg.path, msg.content || "");
      break;

    case "agent_file_saved":
      if (isActive(msg.agentId)) {
        applyAgentConfigFiles(msg);
        if (state.configEditor.source === msg.source && state.configEditor.path === msg.path) {
          state.configEditor.dirty = false;
          state.configEditor.content = $("config-file-body")?.value || state.configEditor.content;
        }
        showToast("配置文件已保存");
      }
      break;

    case "models":
      state.models = msg.models || [];
      renderModels();
      syncModelSelects();
      break;

    case "message_committed":
      applyAgentEvent(aid, "message_committed", {
        message: msg.message,
        index: msg.index,
        messageCount: msg.messageCount,
      });
      break;

    case "stream":
      applyAgentEvent(aid, "stream", msg);
      break;

    case "run_started":
      applyAgentEvent(aid, "run_started", msg);
      break;

    case "run_finished":
      applyAgentEvent(aid, "run_finished", { messageCount: msg.messageCount });
      if (isActive(aid)) fetchConversation(aid);
      break;

    case "run_cancelled":
      applyAgentEvent(aid, "run_cancelled", {});
      break;

    case "discussions":
      if (msg.agentId === state.activeAgentId) {
        state.discussions = sortDiscussionsChronological(msg.discussions || []);
        syncExpandedDiscussionId();
        renderDiscussions();
        refreshAllDiscussionHighlights();
      }
      break;

    case "discussion_created":
      state.discussions = sortDiscussionsChronological([
        ...state.discussions,
        msg.discussion,
      ]);
      state.expandedDiscussionId = msg.discussion.id;
      state.maximizedDiscussionId = null;
      if (!state.discussRailOpen) toggleDiscussRail(true);
      renderDiscussions();
      requestAnimationFrame(() => {
        applyDiscussionAnchorHighlight(msg.discussion.anchor, msg.discussion.id);
        scrollToAnchor(msg.discussion.anchor, msg.discussion.id);
      });
      break;

    case "discussion_user_message":
      appendDiscussionMessage(msg.discussionId, msg.message);
      {
        const drt = getDiscussionRuntime(msg.discussionId);
        drt.busy = true;
        drt.streamingText = "";
        drt.segments = [];
        drt.thinkingText = "";
        drt.toolBlocks = [];
        if (!appendDiscussionUserMessageDom(msg.discussionId, msg.message)) {
          renderDiscussions();
        } else {
          expandDiscussion(msg.discussionId);
          updateDiscussionTitleBusy(msg.discussionId, true);
          updateDiscussionStreaming(msg.discussionId, drt);
        }
      }
      break;

    case "discussion_stream":
      applyDiscussionStream(msg.discussionId, msg);
      break;

    case "discussion_finished":
      {
        const drt = getDiscussionRuntime(msg.discussionId);
        drt.busy = false;
        drt.streamingText = "";
        drt.segments = [];
        drt.thinkingText = "";
        drt.toolBlocks = [];
        if (msg.message) appendDiscussionMessage(msg.discussionId, msg.message);
        if (!finalizeDiscussionPanel(msg.discussionId, msg.message)) {
          renderDiscussions();
        }
      }
      break;

    case "discussion_cancelled":
      {
        const runtime = state.discussionRuntimes[msg.discussionId];
        if (runtime) {
          runtime.busy = false;
          runtime.streamingText = "";
          runtime.segments = [];
          runtime.thinkingText = "";
          runtime.toolBlocks = [];
        }
        const panel = getDiscussionPanel(msg.discussionId);
        if (panel) {
          panel.querySelector(".discussion-msg.streaming")?.remove();
          updateDiscussionTitleBusy(msg.discussionId, false);
        }
      }
      break;

    case "discussion_summary_started":
      openSummarySection(msg.discussionId);
      setSummaryRuntime(msg.discussionId, { busy: true, streamingText: "" });
      updateDiscussionSummaryDom(msg.discussionId, { busy: true, text: "" });
      break;

    case "discussion_summary_stream":
      {
        const rt = getSummaryRuntime(msg.discussionId);
        rt.streamingText = (rt.streamingText || "") + (msg.text || "");
        updateDiscussionSummaryDom(msg.discussionId, {
          busy: true,
          text: rt.streamingText,
        });
      }
      break;

    case "discussion_summary_updated":
      {
        const idx = state.discussions.findIndex((d) => d.id === msg.discussion.id);
        if (idx >= 0) state.discussions[idx] = msg.discussion;
        setSummaryRuntime(msg.discussion.id, { busy: false, streamingText: "" });
        updateDiscussionSummaryDom(msg.discussion.id, {
          busy: false,
          text: msg.discussion.summary || "",
        });
        updateDiscussionSummarizeBtn(msg.discussion.id);
      }
      break;

    case "combined_summaries":
      if (msg.agentId === state.activeAgentId) {
        state.combinedSummaries = msg.combinedSummaries || [];
        renderCombinedSummaries();
      }
      break;

    case "combined_summary_started":
      {
        const existing = state.combinedSummaries.find((c) => c.id === msg.combinedSummary.id);
        if (!existing) {
          state.combinedSummaries.unshift(msg.combinedSummary);
        }
        setCombinedSummaryRuntime(msg.combinedSummary.id, {
          busy: true,
          streamingText: "",
          phase: "individual",
          current: 0,
          total: msg.combinedSummary.discussionIds?.length || 0,
        });
        renderCombinedSummaries();
      }
      break;

    case "combined_summary_progress":
      {
        const rt = getCombinedSummaryRuntime(msg.combinedSummaryId);
        rt.phase = msg.phase;
        rt.current = msg.current;
        rt.total = msg.total;
        rt.busy = true;
        updateCombinedSummaryProgress(msg.combinedSummaryId, rt);
      }
      break;

    case "combined_summary_stream":
      {
        const rt = getCombinedSummaryRuntime(msg.combinedSummaryId);
        rt.streamingText = (rt.streamingText || "") + (msg.text || "");
        updateCombinedSummaryDom(msg.combinedSummaryId, {
          busy: true,
          text: rt.streamingText,
          phase: rt.phase,
          current: rt.current,
          total: rt.total,
        });
      }
      break;

    case "combined_summary_updated":
      {
        const idx = state.combinedSummaries.findIndex((c) => c.id === msg.combinedSummary.id);
        if (idx >= 0) state.combinedSummaries[idx] = msg.combinedSummary;
        else state.combinedSummaries.unshift(msg.combinedSummary);
        setCombinedSummaryRuntime(msg.combinedSummary.id, {
          busy: false,
          streamingText: "",
          phase: null,
          current: 0,
          total: 0,
        });
        updateCombinedSummaryDom(msg.combinedSummary.id, {
          busy: false,
          text: msg.combinedSummary.summary || "",
        });
      }
      break;

    case "combined_summary_deleted":
      state.combinedSummaries = state.combinedSummaries.filter(
        (c) => c.id !== msg.combinedSummaryId
      );
      delete state.combinedSummaryRuntimes[msg.combinedSummaryId];
      renderCombinedSummaries();
      break;

    case "discussion_deleted":
      removeDiscussionHighlight(msg.discussionId);
      delete state.discussionRuntimes[msg.discussionId];
      delete state.summaryRuntimes[msg.discussionId];
      state.discussions = state.discussions.filter((d) => d.id !== msg.discussionId);
      state.combinedSummaries = state.combinedSummaries.filter(
        (c) => !(c.discussionIds || []).includes(msg.discussionId)
      );
      state.selectedDiscussionIds.delete(msg.discussionId);
      state.summarySectionOpen.delete(msg.discussionId);
      if (state.expandedDiscussionId === msg.discussionId) {
        state.expandedDiscussionId = null;
      }
      if (state.maximizedDiscussionId === msg.discussionId) {
        state.maximizedDiscussionId = null;
      }
      renderDiscussions();
      renderCombinedSummaries();
      updateDiscussToolbar();
      refreshAllDiscussionHighlights();
      break;

    case "error":
      if (msg.discussionId) {
        const discussionBusy = !!state.discussionRuntimes[msg.discussionId]?.busy;
        const summaryBusy = !!state.summaryRuntimes[msg.discussionId]?.busy;
        const scope = msg.scope || (discussionBusy ? "discussion" : summaryBusy ? "summary" : "discussion");
        if (scope === "discussion" || discussionBusy) {
          const drt = state.discussionRuntimes[msg.discussionId];
          if (drt) {
            drt.busy = false;
            drt.streamingText = "";
            drt.segments = [];
            drt.thinkingText = "";
            drt.toolBlocks = [];
          }
          const panel = getDiscussionPanel(msg.discussionId);
          if (panel) {
            panel.querySelector(".discussion-msg.streaming")?.remove();
            updateDiscussionTitleBusy(msg.discussionId, false);
          }
          showToast(msg.message || "讨论消息发送失败", "error");
        } else {
          setSummaryRuntime(msg.discussionId, { busy: false, streamingText: "" });
          updateDiscussionSummaryDom(msg.discussionId, { busy: false });
          updateDiscussionSummarizeBtn(msg.discussionId);
          showToast(msg.message || "意图生成失败", "error");
        }
      } else if (msg.combinedSummaryId) {
        setCombinedSummaryRuntime(msg.combinedSummaryId, {
          busy: false,
          streamingText: "",
          phase: null,
        });
        updateCombinedSummaryDom(msg.combinedSummaryId, { busy: false });
        showToast(msg.message || "合并意图生成失败", "error");
      }
      if (state.creatingAgent) {
        state.creatingAgent = false;
        $("btn-new-agent").disabled = false;
      }
      if (state.waitHint) state.waitHint = "";
      if (state.savingAgent) {
        state.savingAgent = false;
        resetSaveAgentButton();
        setDrawerSaveStatus("");
      }
      if (msg.agentId) {
        applyAgentEvent(msg.agentId, "run_error", { message: msg.message });
        if (isActive(msg.agentId) && !lastTurnEl()?.querySelector(".msg.error")) {
          showError(msg.message);
        }
      } else if (!msg.discussionId && !msg.combinedSummaryId) {
        showError(msg.message);
        if (state.savingAgent || $("agent-drawer").classList.contains("open")) {
          showToast(msg.message, "error");
        }
      }
      updateChrome();
      renderAgents();
      updateDiscussToolbar();
      break;
  }
}

function applyAgentEvent(agentId, event, payload) {
  const fx = AgentFSM.dispatch(agentId, event, payload);
  const listItem = state.agents.find((a) => a.id === agentId);
  if (listItem) listItem.running = AgentFSM.isRunning(agentId);
  if (payload?.messageCount != null && listItem) listItem.messageCount = payload.messageCount;

  if (fx.needsResync) fetchConversation(agentId);

  if (fx.committed && isActive(agentId)) {
    const message = fx.committed;
    state.activeAgent = { ...(state.activeAgent || {}), id: agentId, messages: fx.agent.messages };
    if (message.role === "assistant" && adoptStreamingAsCommitted(fx.agent, message, fx.agent.messages.length - 1)) {
      followOutput();
    } else {
      if (message.role === "assistant" || message.role === "error") {
        AgentFSM.removeStreamViewBeforeCommit(fx.agent);
      }
      appendMessageDom(message, fx.agent.messages.length - 1);
      if (message.role === "user") state.autoScroll = true;
      scrollToBottom(message.role === "user" || state.autoScroll);
    }
  }

  if (isActive(agentId)) {
    if (fx.agent.phase === AgentFSM.Phase.IDLE && fx.streamChanged) {
      AgentFSM.finalizeStreamView(fx.agent);
    } else if (fx.agent.phase === AgentFSM.Phase.RUNNING && (fx.streamChanged || fx.phaseChanged)) {
      renderAgentRunUI(agentId);
    }
  }

  const streamTick = event === "stream" && !fx.phaseChanged && !fx.committed;
  if (!streamTick) {
    updateChrome();
    renderAgents();
  }
  return fx;
}

function renderAgentRunUI(agentId) {
  const agent = AgentFSM.get(agentId);
  if (!agent || agent.phase !== AgentFSM.Phase.RUNNING) return;
  if (!startStreamingBubble(agent)) return;
  renderActiveStreaming(agent);
}

function restoreLastAgent() {
  if (!state.agents.length) {
    createAgent();
    return;
  }
  const lastId = localStorage.getItem(LAST_AGENT_KEY);
  const target = (lastId && state.agents.find((a) => a.id === lastId)) || state.agents[0];
  selectAgent(target.id);
}

function createAgent() {
  if (!ensureConnected("无法创建 Agent：尚未连接")) return;
  if (state.creatingAgent) return;
  state.creatingAgent = true;
  $("btn-new-agent").disabled = true;
  setWaitHint("正在创建 Agent…");
  if (!send({
    type: "create_agent",
    name: "新 Agent",
    cwd: state.defaultCwd,
    model: state.defaultModel,
  })) {
    state.creatingAgent = false;
    $("btn-new-agent").disabled = false;
    setWaitHint("");
    showToast("创建失败：连接已断开", "error");
  }
}

function selectAgent(id, agentData) {
  if (state.activeAgentId && state.activeAgentId !== id) {
    saveMessageScrollPosition(state.activeAgentId);
  }
  if (state.activeAgentId) AgentFSM.detachView(state.activeAgentId);

  state.activeAgentId = id;
  localStorage.setItem(LAST_AGENT_KEY, id);
  clearComposer();

  if (agentData) {
    loadConversation(agentData);
  } else {
    paintActiveConversation(id);
    fetchConversation(id);
  }
  send({ type: "list_discussions", agentId: id });
  send({ type: "list_combined_summaries", agentId: id });
  state.selectedDiscussionIds = new Set();
  state.summarySectionOpen = new Set();
  renderAgents();
  updateChrome();
}

function paintActiveConversation(agentId) {
  if (!isActive(agentId)) return;
  const machine = AgentFSM.ensure(agentId);
  const summary = state.agents.find((a) => a.id === agentId) || state.activeAgent || {};
  state.activeAgent = { ...summary, ...machine.meta, id: agentId, messages: machine.messages };
  $("model-name").textContent = machine.meta.model || summary.model || state.defaultModel;
  renderMessages(machine.messages, { scroll: "restore" });
  if (machine.phase === AgentFSM.Phase.RUNNING) renderAgentRunUI(agentId);
}

function fetchConversation(agentId) {
  if (!agentId) return;
  send({ type: "get_agent", agentId });
}

function loadConversation(agent) {
  if (!agent?.id) return;
  const fx = AgentFSM.dispatch(agent.id, "snapshot", {
    ...agent,
    running: !!agent.running,
  });
  if (fx.needsResync) {
    fetchConversation(agent.id);
    return;
  }
  if (!isActive(agent.id)) return;
  state.activeAgent = { ...agent, messages: fx.agent.messages };
  $("model-name").textContent = agent.model || state.defaultModel;
  populateAgentDrawer(agent);
  if (fx.messagesChanged) {
    renderMessages(fx.agent.messages, { scroll: "restore" });
    if (fx.agent.phase === AgentFSM.Phase.RUNNING) renderAgentRunUI(agent.id);
  } else {
    paintActiveConversation(agent.id);
  }
  if (state.openDrawerAfterSelect && agent.id === state.activeAgentId) {
    state.openDrawerAfterSelect = false;
    showAgentDrawer();
  }
}

function populateAgentDrawer(agent) {
  $("agent-name-input").value = agent.name || "";
  $("agent-cwd-input").value = agent.cwd || "";
  fillModelSelect($("agent-model-input"), agent.model || state.defaultModel);
  $("rules-dir-input").value = agent.rulesDir || "";
  $("skills-dir-input").value = agent.skillsDir || "";
  $("memory-dir-input").value = agent.memoryDir || "";
  $("toggle-soul").checked = !!agent.enableSoul;
  $("toggle-rules").checked = !!agent.enableRules;
  $("toggle-skills").checked = !!agent.enableSkills;
  $("toggle-memory").checked = !!agent.enableMemory;
  setDrawerSaveStatus("");
}

function setDrawerSaveStatus(text) {
  const el = $("drawer-save-status");
  if (el) el.textContent = text || "";
}

function resetSaveAgentButton() {
  const btn = $("btn-save-agent");
  if (!btn) return;
  btn.disabled = false;
  btn.textContent = "保存 Agent 配置";
}

function showToast(message, kind = "ok") {
  const el = $("toast");
  if (!el) return;
  el.textContent = message;
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.add("hidden"), 2800);
}

async function pickFolder(initialDir) {
  const api = window.pywebview?.api;
  if (!api?.pick_folder) {
    showToast("请使用桌面应用打开以选择目录", "error");
    return null;
  }
  try {
    const chosen = await api.pick_folder(initialDir || "");
    return chosen || null;
  } catch (err) {
    showToast("选择目录失败", "error");
    return null;
  }
}

async function bindPickFolderButton(buttonId, inputId) {
  const btn = $(buttonId);
  const input = $(inputId);
  if (!btn || !input) return;
  btn.onclick = async () => {
    const chosen = await pickFolder(input.value.trim());
    if (chosen) input.value = chosen;
  };
}

function bindClearPathButton(buttonId, inputId) {
  const btn = $(buttonId);
  const input = $(inputId);
  if (!btn || !input) return;
  btn.onclick = () => {
    input.value = "";
  };
}

function loadAgentSoul() {
  if (!state.activeAgentId) return;
  send({ type: "read_agent_files", agentId: state.activeAgentId });
}

function applyAgentConfigFiles(msg) {
  if (msg.soul != null) state.configFiles.soul = msg.soul;
  if (msg.rulesTree) state.configFiles.rulesTree = msg.rulesTree;
  if (msg.skillsTree) state.configFiles.skillsTree = msg.skillsTree;
  if (msg.memoryTree) state.configFiles.memoryTree = msg.memoryTree;
  renderConfigFileTree("rules", state.configFiles.rulesTree);
  renderConfigFileTree("skills", state.configFiles.skillsTree);
  renderConfigFileTree("memory", state.configFiles.memoryTree);
  syncConfigEditorLabel();
}

function flattenConfigTree(nodes, acc = []) {
  for (const node of nodes || []) {
    if (node.type === "file") acc.push(node.path);
    if (node.children?.length) flattenConfigTree(node.children, acc);
  }
  return acc;
}

function renderConfigFileTree(source, nodes) {
  const el = $(`${source}-tree`);
  if (!el) return;
  const files = flattenConfigTree(nodes);
  el.innerHTML = "";
  if (!files.length) {
    el.innerHTML = '<p class="config-file-empty">暂无文件</p>';
    return;
  }
  for (const path of files) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "config-file-item";
    if (state.configEditor.source === source && state.configEditor.path === path) {
      btn.classList.add("active");
    }
    btn.textContent = path;
    btn.title = path;
    btn.onclick = () => requestConfigFile(source, path);
    el.appendChild(btn);
  }
}

function requestConfigFile(source, path) {
  if (!state.activeAgentId || !path) return;
  if (state.configEditor.dirty && !confirm("当前文件有未保存修改，确定切换？")) return;
  send({ type: "read_agent_file", agentId: state.activeAgentId, source, path });
}

function openConfigFileEditor(source, path, content) {
  state.configEditor = { source, path, content, dirty: false };
  const wrap = $("config-file-editor");
  const body = $("config-file-body");
  if (wrap) wrap.classList.remove("hidden");
  if (body) body.value = content;
  syncConfigEditorLabel();
  renderConfigFileTree("rules", state.configFiles.rulesTree);
  renderConfigFileTree("skills", state.configFiles.skillsTree);
  renderConfigFileTree("memory", state.configFiles.memoryTree);
}

function syncConfigEditorLabel() {
  const label = $("config-file-label");
  if (!label) return;
  const { source, path, dirty } = state.configEditor;
  label.textContent = path ? `${source} / ${path}${dirty ? " *" : ""}` : "";
}

function saveConfigFile() {
  if (!state.activeAgentId || !state.configEditor.path) {
    showToast("请先选择要保存的文件", "error");
    return;
  }
  if (!ensureConnected()) return;
  const content = $("config-file-body")?.value ?? "";
  send({
    type: "write_agent_file",
    agentId: state.activeAgentId,
    source: state.configEditor.source,
    path: state.configEditor.path,
    content,
  });
}

function createConfigFile(source) {
  if (!state.activeAgentId) return;
  const raw = prompt("新文件名（相对路径，如 notes.md）", `${source}.md`);
  const path = String(raw || "").trim().replace(/^\/+/, "");
  if (!path) return;
  if (path.includes("..")) {
    showToast("非法路径", "error");
    return;
  }
  if (!ensureConnected()) return;
  send({
    type: "write_agent_file",
    agentId: state.activeAgentId,
    source,
    path,
    content: "",
  });
  openConfigFileEditor(source, path, "");
}

function renderAgents() {
  const list = $("agent-list");
  list.innerHTML = "";
  for (const a of state.agents) {
    const running = agentSummaryRunning(a.id);
    const el = document.createElement("div");
    el.className =
      "agent-item" +
      (a.id === state.activeAgentId ? " active" : "") +
      (running ? " running" : "");
    el.innerHTML = `
      <div class="agent-main">
        <div class="title">
          ${running ? '<span class="agent-running-dot" title="运行中"></span>' : ""}
          <span class="agent-title-text">${escapeHtml(a.name)}</span>
        </div>
        <div class="meta">${escapeHtml(a.cwd)}</div>
      </div>
      <button class="agent-settings" type="button" title="Agent 设置">⚙</button>
      <button class="agent-delete" type="button" title="删除 Agent">×</button>`;
    el.querySelector(".agent-main").onclick = () => selectAgent(a.id);
    el.querySelector(".agent-settings").onclick = (e) => {
      e.stopPropagation();
      openAgentDrawer(a.id);
    };
    el.querySelector(".agent-delete").onclick = (e) => {
      e.stopPropagation();
      deleteAgent(a.id, a.name);
    };
    list.appendChild(el);
  }
}

function deleteAgent(id, name) {
  if (!confirm(`确定删除 Agent「${name}」？主对话与讨论将一并删除。`)) return;
  if (!ensureConnected("无法删除：尚未连接")) return;
  setWaitHint("正在删除 Agent…");
  if (!send({ type: "delete_agent", agentId: id })) {
    setWaitHint("");
    showToast("删除失败：连接已断开", "error");
  }
}

function resetAgent(id, name) {
  if (
    !confirm(
      `确定重置 Agent「${name}」？\n\n将清空主对话、所有讨论，并删除 .agent/uploads 与 .agent/outputs 中的产物。Soul、Rules 等配置不会删除。`
    )
  ) {
    return;
  }
  if (!ensureConnected("无法重置：尚未连接")) return;
  closeAgentDrawer();
  setWaitHint("正在重置 Agent…");
  if (!send({ type: "reset_agent", agentId: id })) {
    setWaitHint("");
    showToast("重置失败：连接已断开", "error");
  }
}

function deleteDiscussion(discussionId) {
  if (!confirm("确定删除此讨论？")) return;
  send({ type: "delete_discussion", discussionId });
}

function renderMessages(messages, { scroll = "restore" } = {}) {
  const box = $("messages");
  if (!messages.length) {
    box.innerHTML = `<div class="empty-state">
      <p>向 Agent 提问，它会读取工作目录中的代码并执行任务。</p>
      <p style="margin-top:12px;font-size:13px;">每个 Agent 仅一条主对话线。选中文字可发起讨论。</p>
    </div>`;
    clearAnchorHighlights();
    return;
  }
  const frag = document.createDocumentFragment();
  for (const turn of groupConversationTurns(messages)) {
    const wrap = document.createElement("div");
    wrap.className = "turn";
    for (const item of turn) {
      try {
        wrap.appendChild(buildMessageEl(item.message, item.index));
      } catch (err) {
        console.error("render message failed", item.index, err);
        const el = document.createElement("div");
        el.className = `msg ${item.message.role || "assistant"}`;
        el.dataset.messageIndex = String(item.index);
        el.innerHTML = `<div class="body"><pre>${escapeHtml(item.message.content || "")}</pre></div>`;
        wrap.appendChild(el);
      }
    }
    frag.appendChild(wrap);
  }
  box.innerHTML = "";
  box.appendChild(frag);
  refreshAllDiscussionHighlights();
  if (scroll === "bottom") {
    scrollToBottom(true);
  } else if (scroll === "restore") {
    restoreMessageScrollPosition(state.activeAgentId);
  }
}

function groupConversationTurns(messages) {
  const turns = [];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    const last = turns[turns.length - 1];
    const startNewTurn =
      message.role === "user" ||
      !last ||
      last.some((item) => item.message.role === "assistant" || item.message.role === "error");
    if (startNewTurn) turns.push([]);
    turns[turns.length - 1].push({ message, index });
  }
  return turns;
}

function lastTurnEl() {
  const box = $("messages");
  const turns = box?.querySelectorAll(":scope > .turn");
  return turns?.length ? turns[turns.length - 1] : null;
}

function userTurnAwaitingReply() {
  const turn = lastTurnEl();
  if (!turn) return null;
  if (
    turn.querySelector(":scope > .msg.user") &&
    !turn.querySelector(":scope > .msg.assistant, :scope > .msg.error")
  ) {
    return turn;
  }
  return null;
}

function removeTurnIfEmpty(turn) {
  if (!turn || !turn.classList.contains("turn")) return;
  if (turn.querySelector(".msg")) return;
  turn.remove();
}

function placeStreamAfterUser(streamEl, turn) {
  if (!streamEl || !turn) return;
  const user = turn.querySelector(":scope > .msg.user");
  const oldTurn = streamEl.parentElement;
  if (user) user.insertAdjacentElement("afterend", streamEl);
  else if (streamEl.parentElement !== turn) turn.appendChild(streamEl);
  if (oldTurn && oldTurn !== turn) removeTurnIfEmpty(oldTurn);
}

function attachStreamToUserTurn(streamEl, userTurn) {
  placeStreamAfterUser(streamEl, userTurn);
}

function scrollStorageKey(agentId) {
  return `${SCROLL_PREFIX}${agentId}`;
}

function saveMessageScrollPosition(agentId) {
  if (!agentId) return;
  const box = $("messages");
  if (!box || box.querySelector(".empty-state")) return;
  const payload = {
    top: box.scrollTop,
    atBottom: isNearBottom(box),
    count: box.querySelectorAll(".msg").length,
  };
  try {
    localStorage.setItem(scrollStorageKey(agentId), JSON.stringify(payload));
  } catch {
    /* ignore quota errors */
  }
}

function restoreMessageScrollPosition(agentId) {
  if (!agentId) return;
  const box = $("messages");
  if (!box) return;

  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(scrollStorageKey(agentId)) || "null");
  } catch {
    saved = null;
  }

  const apply = () => {
    if (!saved) {
      scrollToBottom(true);
      return;
    }
    if (saved.atBottom) {
      scrollToBottom(true);
      return;
    }
    state.suppressScrollAutoUpdate = true;
    const maxTop = Math.max(0, box.scrollHeight - box.clientHeight);
    box.scrollTop = Math.min(Math.max(0, Number(saved.top) || 0), maxTop);
    state.autoScroll = isNearBottom(box);
    state.suppressScrollAutoUpdate = false;
  };

  apply();
}

function legacyMessageToSegments(message) {
  const segments = [];
  if (message.content) segments.push({ type: "text", text: message.content });
  for (const block of message.blocks || []) {
    if (block.type === "tool_result") continue;
    if (block.type === "thinking") {
      segments.push({ type: "thinking", text: block.text || "" });
    } else if (block.type === "tool" || block.type === "tool_call") {
      segments.push({ ...block, type: "tool" });
    }
  }
  return segments;
}

function resolveAssistantSegments(message) {
  if (message.segments?.length) return message.segments;
  return legacyMessageToSegments(message);
}

function groupAssistantRenderGroups(segments) {
  const groups = [];
  for (const seg of segments || []) {
    if (seg.type === "text") {
      groups.push({ type: "text", seg });
      continue;
    }
    if (seg.type === "thinking" || seg.type === "tool") {
      const last = groups[groups.length - 1];
      if (last?.type === "trace") last.segments.push(seg);
      else groups.push({ type: "trace", segments: [seg] });
    }
  }
  return groups;
}

function traceStatusText(segments, { streaming = false, activityText = "" } = {}) {
  const tools = (segments || []).filter((seg) => seg.type === "tool");
  const thinking = (segments || []).filter((seg) => seg.type === "thinking");
  const running = tools.find((seg) => seg.status === "running");
  if (streaming) {
    if (running) return running.label || running.name || "运行中…";
    const live = String(activityText || "").trim();
    if (live) return live;
    if (thinking.length) return "思考中…";
    return "Agent 运行中…";
  }
  if (thinking.length && tools.length) return `思考 · ${tools.length} 个步骤`;
  if (tools.length === 1) return tools[0].label || tools[0].name || "1 个步骤";
  if (tools.length) return `${tools.length} 个步骤`;
  if (thinking.length) return "思考";
  return "步骤";
}

function buildTraceCard(segments, { open = false, streaming = false, activityText = "" } = {}) {
  const details = document.createElement("details");
  details.className = "trace-card" + (streaming ? " running" : "");
  details.open = !!open;
  const summary = document.createElement("summary");
  summary.className = "trace-summary";
  const status = document.createElement("span");
  status.className = "trace-status";
  status.textContent = traceStatusText(segments, { streaming, activityText });
  summary.appendChild(status);
  details.appendChild(summary);
  const body = document.createElement("div");
  body.className = "trace-body";
  details.appendChild(body);
  patchTraceBody(body, segments, { streaming, activityText });
  return details;
}

function patchTraceBody(bodyEl, segments, { streaming = false, activityText = "" } = {}) {
  if (!bodyEl) return;
  const wanted = (segments || []).filter((seg) => seg.type === "thinking" || seg.type === "tool");
  let index = 0;

  // 还没有任何思考/工具时，展开也应看到当前状态，避免「外面 running、里面空白」
  if (!wanted.length && streaming) {
    let child = bodyEl.children[0];
    if (!child || !child.classList.contains("trace-activity")) {
      const el = document.createElement("div");
      el.className = "trace-activity";
      if (child) bodyEl.insertBefore(el, child);
      else bodyEl.appendChild(el);
      child = el;
    }
    const label = String(activityText || "").trim() || "Agent 运行中…";
    if (child.textContent !== label) child.textContent = label;
    index = 1;
    while (bodyEl.children.length > index) bodyEl.removeChild(bodyEl.lastChild);
    return;
  }

  for (const seg of wanted) {
    let child = bodyEl.children[index];
    if (seg.type === "thinking") {
      if (!child || !child.classList.contains("thinking-body")) {
        const el = document.createElement("div");
        el.className = "thinking-body";
        if (child) bodyEl.insertBefore(el, child);
        else bodyEl.appendChild(el);
        child = el;
      }
      if (child.textContent !== (seg.text || "")) child.textContent = seg.text || "";
    } else {
      if (!child || !child.classList.contains("tool-card")) {
        const el = buildLiveToolCard(seg);
        if (child) bodyEl.insertBefore(el, child);
        else bodyEl.appendChild(el);
        child = el;
      } else {
        child.className = "tool-card" + (seg.status === "running" ? " running" : " done");
        const label = child.querySelector(".tool-label");
        const next = seg.label || seg.name || "";
        if (label && label.textContent !== next) label.textContent = next;
      }
    }
    index += 1;
  }
  while (bodyEl.children.length > index) bodyEl.removeChild(bodyEl.lastChild);
}

function patchTraceCard(details, segments, { streaming = false, activityText = "" } = {}) {
  details.classList.toggle("running", !!streaming);
  const status = details.querySelector(".trace-status");
  const next = traceStatusText(segments, { streaming, activityText });
  if (status && status.textContent !== next) status.textContent = next;
  patchTraceBody(details.querySelector(".trace-body"), segments, { streaming, activityText });
}

function assistantRenderNodes(message, { streaming = false } = {}) {
  const groups = groupAssistantRenderGroups(resolveAssistantSegments(message));
  if (!streaming) return groups;
  const texts = [];
  const traceSegs = [];
  for (const group of groups) {
    if (group.type === "trace") traceSegs.push(...group.segments);
    else texts.push(group);
  }
  return [{ type: "trace", segments: traceSegs }, ...texts];
}

function patchStreamMarkdown(el, text, streaming) {
  const next = String(text || "");
  if (streaming) {
    if (el.dataset.md === next && el.dataset.mode === "plain") return;
    el.dataset.md = next;
    el.dataset.mode = "plain";
    delete el.dataset.stable;
    delete el.dataset.final;
    el.classList.add("streaming-plain");
    el.textContent = next;
    return;
  }
  if (el.dataset.md === next && el.dataset.final === "1") return;
  el.dataset.md = next;
  el.dataset.final = "1";
  el.dataset.mode = "md";
  el.classList.remove("streaming-plain");
  delete el.dataset.stable;
  el.innerHTML = renderMarkdown(next);
  enhanceMarkdown(el);
}

function patchAssistantBody(body, message, { streaming = false, activityText = "" } = {}) {
  const nodes = assistantRenderNodes(message, { streaming });
  let index = 0;
  for (const node of nodes) {
    let child = body.children[index];
    if (node.type === "trace") {
      if (!child || !child.classList.contains("trace-card")) {
        const el = buildTraceCard(node.segments, { streaming, activityText });
        if (child) body.insertBefore(el, child);
        else body.appendChild(el);
        child = el;
      } else {
        patchTraceCard(child, node.segments, { streaming, activityText });
      }
    } else {
      if (!child || !child.classList.contains("stream-text")) {
        const el = document.createElement("div");
        el.className = "stream-text";
        if (child) body.insertBefore(el, child);
        else body.appendChild(el);
        child = el;
      }
      patchStreamMarkdown(child, node.seg.text || "", streaming);
    }
    index += 1;
  }
  while (body.children.length > index) body.removeChild(body.lastChild);
}

function renderAssistantSegment(body, seg) {
  if (seg.type === "text") {
    const textEl = document.createElement("div");
    textEl.className = "stream-text";
    textEl.innerHTML = renderMarkdown(seg.text || "");
    enhanceMarkdown(textEl);
    body.appendChild(textEl);
    return;
  }
  if (seg.type === "tool") {
    body.appendChild(buildLiveToolCard(seg));
  }
}

function renderAssistantBody(body, message, { streaming = false, activityText = "" } = {}) {
  if (streaming) {
    patchAssistantBody(body, message, { streaming: true, activityText });
    return;
  }
  body.innerHTML = "";
  const groups = groupAssistantRenderGroups(resolveAssistantSegments(message));
  for (const group of groups) {
    if (group.type === "trace") {
      body.appendChild(buildTraceCard(group.segments, { activityText }));
    } else {
      renderAssistantSegment(body, group.seg);
    }
  }
}

function adoptStreamingAsCommitted(agent, message, index) {
  const el = agent?.view?.streamingEl;
  if (!el || !el.isConnected) return false;
  el.className = "msg assistant";
  el.dataset.messageIndex = String(index ?? "");
  const body = el.querySelector(".body");
  if (body) {
    body.classList.remove("streaming");
    patchAssistantBody(body, message, { streaming: false });
  }
  agent.view.streamingEl = null;
  agent.view.thinkingEl = null;
  return true;
}

function buildMessageEl(m, messageIndex) {
  const el = document.createElement("div");
  el.className = `msg ${m.role}`;
  el.dataset.messageIndex = String(messageIndex ?? "");
  if (m.role === "error") {
    el.innerHTML = `<div class="role">错误</div><div class="body">${escapeHtml(m.content || "")}</div>`;
    return el;
  }
  el.innerHTML = `<div class="body"></div>`;
  const body = el.querySelector(".body");
  if (m.role === "assistant") {
    renderAssistantBody(body, m);
  } else {
    const inlineBody = document.createElement("div");
    inlineBody.className = "msg-inline-body";
    renderInlineBody(inlineBody, m.content, m.attachments);
    body.appendChild(inlineBody);
  }
  return el;
}

function buildLiveToolCard(block) {
  const el = document.createElement("div");
  el.className = "tool-card" + (block.status === "running" ? " running" : " done");
  el.innerHTML = `<span class="tool-label">${escapeHtml(block.label || block.name)}</span>`;
  return el;
}

function startStreamingBubble(agent) {
  const box = $("messages");
  if (!box) return false;
  const empty = box.querySelector(".empty-state");
  if (empty) empty.remove();
  const waiting = userTurnAwaitingReply();
  if (agent.view.streamingEl && box.contains(agent.view.streamingEl)) {
    const last = lastTurnEl();
    if (last?.querySelector(":scope > .msg.user")) {
      placeStreamAfterUser(agent.view.streamingEl, last);
    }
    return true;
  }
  if (!waiting) return false;
  state.autoScroll = true;
  agent.view.streamingEl = document.createElement("div");
  agent.view.streamingEl.className = "msg assistant";
  agent.view.streamingEl.innerHTML = '<div class="body streaming"></div>';
  placeStreamAfterUser(agent.view.streamingEl, waiting);
  patchAssistantBody(
    agent.view.streamingEl.querySelector(".body"),
    { segments: [] },
    { streaming: true, activityText: agent.stream.activityText || "" }
  );
  followOutput();
  return true;
}

const streamPaintFrames = new Map();

function renderActiveStreaming(agent) {
  if (!agent?.id) return;
  if (!startStreamingBubble(agent)) return;
  if (streamPaintFrames.has(agent.id)) return;
  streamPaintFrames.set(
    agent.id,
    requestAnimationFrame(() => {
      streamPaintFrames.delete(agent.id);
      const live = AgentFSM.get(agent.id);
      if (!live || live.phase !== AgentFSM.Phase.RUNNING) return;
      if (!startStreamingBubble(live)) return;
      const body = live.view.streamingEl.querySelector(".body");
      if (!body) return;
      patchAssistantBody(
        body,
        { segments: live.stream.segments || [] },
        { streaming: true, activityText: live.stream.activityText || "" }
      );
      followOutput();
    })
  );
}

function appendMessageDom(message, index) {
  const box = $("messages");
  const empty = box.querySelector(".empty-state");
  if (empty) empty.remove();
  const el = buildMessageEl(message, index);
  if (message.role === "user") {
    const turn = document.createElement("div");
    turn.className = "turn";
    turn.appendChild(el);
    box.appendChild(turn);
    const agent = state.activeAgentId ? AgentFSM.get(state.activeAgentId) : null;
    if (agent?.view?.streamingEl && box.contains(agent.view.streamingEl)) {
      attachStreamToUserTurn(agent.view.streamingEl, turn);
    } else if (agent && agent.phase === AgentFSM.Phase.RUNNING) {
      startStreamingBubble(agent);
      renderActiveStreaming(agent);
    }
    return;
  }
  if (message.role === "error") {
    let turn = lastTurnEl();
    if (!turn) {
      turn = document.createElement("div");
      turn.className = "turn";
      box.appendChild(turn);
    }
    turn.appendChild(el);
    return;
  }
  let turn = userTurnAwaitingReply() || lastTurnEl();
  if (!turn || turn.querySelector(".msg.assistant, .msg.error")) {
    turn = document.createElement("div");
    turn.className = "turn";
    box.appendChild(turn);
  }
  turn.appendChild(el);
}

function showError(message) {
  const box = $("messages");
  const empty = box.querySelector(".empty-state");
  if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = "msg error";
  el.innerHTML = `<div class="role">错误</div><div class="body">${escapeHtml(message)}</div>`;
  let turn = lastTurnEl();
  if (!turn) {
    turn = document.createElement("div");
    turn.className = "turn";
    box.appendChild(turn);
  }
  turn.appendChild(el);
  scrollToBottom(true);
}

function renderBlock(block) {
  const el = document.createElement("div");
  if (block.type === "thinking") {
    el.className = "thinking-card-static";
    el.innerHTML = `<details><summary>思考</summary><div class="thinking-body">${escapeHtml(block.text || "")}</div></details>`;
  } else if (block.type === "tool" || block.type === "tool_call") {
    el.className = "tool-card done";
    el.innerHTML = `<span class="tool-label">${escapeHtml(block.label || block.name)}</span>`;
  }
  return el;
}

function renderMarkdown(text) {
  if (!text) return "";
  const prepared = linkifyBareImagePaths(text);
  if (window.marked) {
    try {
      return marked.parse(prepared);
    } catch (err) {
      console.error("markdown render failed", err);
      return `<pre>${escapeHtml(text)}</pre>`;
    }
  }
  return `<pre>${escapeHtml(text)}</pre>`;
}

const BARE_IMAGE_PATH_RE =
  /(^|[\s(>])(~\/\S+?\.(?:png|jpe?g|gif|webp|bmp|svg|heic|heif)|\/(?:Users|tmp|var|private)[^\s)<>\]]+?\.(?:png|jpe?g|gif|webp|bmp|svg|heic|heif))(?=$|[\s),<\]])/gi;

function linkifyBareImagePaths(text) {
  return String(text).replace(BARE_IMAGE_PATH_RE, (match, prefix, path) => `${prefix}![](${path})`);
}

function resolveMediaUrl(href) {
  const raw = String(href || "").trim();
  if (!raw) return { src: "", display: "" };
  if (/^data:image\//i.test(raw)) {
    return { src: raw, display: "（内嵌图片）" };
  }
  if (/^https?:\/\//i.test(raw) || raw.startsWith("//")) {
    const src = raw.startsWith("//") ? `https:${raw}` : raw;
    return { src, display: raw };
  }
  if (/^file:\/\//i.test(raw)) {
    return { src: "", display: decodeURIComponent(raw.replace(/^file:\/\//i, "")) };
  }
  return { src: "", display: raw };
}

function enhanceMarkdown(root) {
  if (!root) return;
  root.classList.add("markdown-body");
  for (const img of root.querySelectorAll(".msg-image-media")) {
    img.addEventListener(
      "error",
      () => {
        img.classList.add("broken");
      },
      { once: true }
    );
  }
  if (!window.hljs) return;
  for (const block of root.querySelectorAll("pre code")) {
    if (block.classList.contains("hljs")) continue;
    window.hljs.highlightElement(block);
  }
}

function setAssistantMarkdownBody(body, text) {
  body.innerHTML = renderMarkdown(text || "");
  enhanceMarkdown(body);
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function isNearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}

function followOutput() {
  const box = $("messages");
  if (!box || !state.autoScroll) return;
  const maxScroll = Math.max(0, box.scrollHeight - box.clientHeight);
  if (maxScroll <= box.scrollTop + 1) return;
  state.suppressScrollAutoUpdate = true;
  box.scrollTop = maxScroll;
  state.suppressScrollAutoUpdate = false;
}

function scrollToBottom(force) {
  const box = $("messages");
  if (!box) return;
  if (!force && !state.autoScroll) return;
  state.suppressScrollAutoUpdate = true;
  box.scrollTop = Math.max(0, box.scrollHeight - box.clientHeight);
  state.suppressScrollAutoUpdate = false;
}

function isUploading() {
  return !!mainComposer?.isBlocked();
}

function updateChrome() {
  const waitText = currentWaitText();
  const waiting = !!waitText;
  const dot = $("status-dot");
  dot.className = "";
  if (isActiveBusy() || waiting) dot.classList.add("busy");
  else if (state.connected) dot.classList.add("connected");
  dot.title = waitText || (state.connected ? "已连接" : "未连接");

  const btn = $("btn-send");
  const uploading = isUploading();
  const stopping = isActiveBusy();
  btn.classList.toggle("stop", stopping && !uploading);
  btn.classList.toggle("is-disabled", uploading);
  if (uploading) {
    btn.textContent = "上传中…";
    btn.disabled = true;
  } else if (stopping) {
    btn.textContent = "停止";
    btn.disabled = false;
  } else {
    btn.textContent = "发送";
    btn.disabled = false;
  }
}

function currentWaitText() {
  if (isActiveBusy()) {
    const machine = getActiveAgent();
    return machine?.stream?.activityText || "Agent 运行中…";
  }
  if (state.waitHint) return state.waitHint;
  return "";
}

function setWaitHint(text) {
  state.waitHint = text || "";
  updateChrome();
}

function focusMessageInput() {
  mainComposer?.focus();
}

function onComposerAction() {
  if (isUploading() || !mainComposer) return;
  if (isActiveBusy()) cancelRun();
  else sendMessage();
}

function cancelRun() {
  if (!state.activeAgentId || !isActiveBusy()) return;
  if (!ensureConnected("无法停止：尚未连接")) return;
  const btn = $("btn-send");
  btn.disabled = true;
  send({ type: "cancel", agentId: state.activeAgentId });
}

async function sendMessage() {
  if (isActiveBusy() || isUploading() || !mainComposer) return;
  if (!state.activeAgentId) {
    showToast("请先选择 Agent", "error");
    return;
  }
  if (!ensureConnected("尚未连接到 Agent 服务，消息未发送")) return;
  try {
    await mainComposer.uploadPending(state.activeAgentId);
  } catch (err) {
    showToast(err.message || "附件上传失败", "error");
    return;
  }

  const { plain, serialized, attachments } = mainComposer.serialize();
  if (!plain.trim() && !attachments.length) return;

  const payload = {
    type: "send",
    agentId: state.activeAgentId,
    message: plain,
    content: serialized || undefined,
    attachments,
  };
  if (!send(payload)) {
    showToast("发送失败：连接已断开", "error");
    return;
  }

  const activity = state.engineState === "ready" ? "Agent 运行中…" : "正在启动引擎…";
  applyAgentEvent(state.activeAgentId, "run_started", { activity });

  state.autoScroll = true;
  mainComposer.clear();
  focusMessageInput();
}

function clearComposer() {
  mainComposer?.clear();
}

function appendInlineText(container, text) {
  if (!text) return;
  const parts = text.split("\n");
  parts.forEach((part, i) => {
    if (part) container.appendChild(document.createTextNode(part));
    if (i < parts.length - 1) container.appendChild(document.createElement("br"));
  });
}

function formatAttachmentLabel(item) {
  if (item.path) {
    const parts = String(item.path).replace(/\/+$/, "").split("/");
    const base = parts.pop() || item.name || "引用";
    return item.kind === "directory" ? `${base}/` : base;
  }
  return item.name || "引用";
}

function attachmentDisplayPath(item) {
  return String(item?.path || item?.relative || "").trim();
}

function attachmentFromChip(chip) {
  return {
    path: chip.dataset.path || "",
    kind: chip.dataset.kind || "file",
    name: chip.dataset.name || formatAttachmentLabel({ path: chip.dataset.path, kind: chip.dataset.kind }),
  };
}

function createAttachmentPathEl(item, chipRef) {
  const path = attachmentDisplayPath(item);
  const label = formatAttachmentLabel(item);
  const pathEl = document.createElement("span");
  pathEl.className = "attachment-path-text";
  pathEl.textContent = label;
  pathEl.title = path ? `${path}\n（点击显示完整路径）` : label;

  if (path) {
    pathEl.addEventListener("click", (e) => {
      e.stopPropagation();
      const chip = chipRef || pathEl.closest(".inline-attachment");
      if (!chip) return;
      const showing = chip.classList.toggle("show-full-path");
      pathEl.textContent = showing ? path : label;
    });
  }
  return pathEl;
}

function createInlineAttachmentChip(item, options = {}) {
  const chip = document.createElement("span");
  chip.className = "inline-attachment attachment-chip";
  chip.contentEditable = "false";
  const path = attachmentDisplayPath(item);
  if (path) {
    chip.dataset.path = path;
    chip.dataset.kind = item.kind || "file";
    chip.dataset.name = item.name || formatAttachmentLabel(item);
  }
  chip.appendChild(createAttachmentPathEl(item, chip));

  if (options.removable) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "×";
    btn.title = "移除";
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      chip.remove();
      options.onRemove?.();
    });
    chip.appendChild(btn);
  }
  return chip;
}

function resolveAttachmentItem(attachMap, path) {
  if (attachMap.has(path)) return attachMap.get(path);
  const base = String(path).replace(/\/+$/, "").split("/").pop();
  if (base) {
    for (const [key, item] of attachMap) {
      if (key.endsWith(`/${base}`) || key === base) return item;
    }
  }
  return {
    path,
    kind: path.endsWith("/") ? "directory" : "file",
  };
}

function renderInlineBody(container, content, attachments) {
  container.innerHTML = "";
  const attachList = attachments || [];
  const attachMap = new Map();
  for (const a of attachList) {
    const p = attachmentDisplayPath(a);
    if (p) attachMap.set(p, a);
  }

  const hasMarkers = content && /\[\[πattach:(.*?)\]\]/.test(content);
  if (hasMarkers) {
    const re = /\[\[πattach:(.*?)\]\]/g;
    let last = 0;
    let m;
    while ((m = re.exec(content)) !== null) {
      if (m.index > last) appendInlineText(container, content.slice(last, m.index));
      container.appendChild(createInlineAttachmentChip(resolveAttachmentItem(attachMap, m[1])));
      last = re.lastIndex;
    }
    if (last < content.length) appendInlineText(container, content.slice(last));
    return;
  }

  if (content) appendInlineText(container, content);
  for (const a of attachList) {
    container.appendChild(createInlineAttachmentChip(a));
  }
}

function serializeInlineRoot(root) {
  let serialized = "";
  let plain = "";
  const attachments = [];
  const seen = new Set();

  function walk(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      serialized += node.textContent;
      plain += node.textContent;
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.classList?.contains("inline-attachment")) {
      const path = node.dataset?.path || node.getAttribute?.("data-path") || "";
      if (path) {
        if (!seen.has(path)) {
          seen.add(path);
          attachments.push(
            node.dataset?.path
              ? attachmentFromChip(node)
              : {
                  path,
                  kind: node.getAttribute("data-kind") || "file",
                  name: node.getAttribute("data-name") || formatAttachmentLabel({ path }),
                }
          );
        }
        serialized += encodeAttachmentMarker(path);
      }
      return;
    }
    if (node.tagName === "BR") {
      serialized += "\n";
      plain += "\n";
      return;
    }
    for (const child of node.childNodes) walk(child);
  }

  for (const child of root.childNodes) walk(child);
  return {
    serialized: serialized.trim(),
    plain: plain.trim(),
    attachments,
  };
}

function serializeSelection(sel) {
  if (!sel?.rangeCount) return { serialized: "", plain: "", attachments: [] };
  return serializeInlineRoot(sel.getRangeAt(0).cloneContents());
}

function buildDiscussionAnchorEl(anchor, discussionId) {
  const anchorEl = document.createElement("div");
  anchorEl.className = "discussion-anchor";
  anchorEl.title = "点击定位到主对话";
  const inlineBody = document.createElement("div");
  inlineBody.className = "msg-inline-body discussion-inline-body discussion-anchor-body";
  const quoteContent = anchor?.quoteContent || anchor?.quote || "";
  renderInlineBody(inlineBody, quoteContent, anchor?.attachments || []);
  anchorEl.appendChild(inlineBody);
  anchorEl.onclick = (e) => {
    e.stopPropagation();
    scrollToAnchor(anchor, discussionId);
  };
  return anchorEl;
}

function sortDiscussionsChronological(list) {
  return [...list].sort((a, b) => {
    const ta = a.createdAt || a.updatedAt || "";
    const tb = b.createdAt || b.updatedAt || "";
    return ta.localeCompare(tb);
  });
}

function scrollDiscussionPanelsToBottom() {
  requestAnimationFrame(() => {
    const msgBox = document.querySelector(".discussion-panel.maximized .discussion-messages")
      || document.querySelector(".discussion-panel.expanded .discussion-messages");
    if (msgBox) {
      msgBox.scrollTop = msgBox.scrollHeight;
      return;
    }
    const container = $("discussion-panels");
    if (container) container.scrollTop = container.scrollHeight;
  });
}

function fragmentToPlainAndAttachments(fragment) {
  let text = "";
  const attachments = [];
  const seen = new Set();

  function walk(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      text += node.textContent;
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.classList?.contains("inline-attachment")) {
      const path = node.dataset.path || "";
      text += path;
      if (path && !seen.has(path)) {
        seen.add(path);
        attachments.push(attachmentFromChip(node));
      }
      return;
    }
    if (node.tagName === "BR") {
      text += "\n";
      return;
    }
    if ((node.tagName === "DIV" || node.tagName === "P") && text.length && !text.endsWith("\n")) {
      text += "\n";
    }
    for (const child of node.childNodes) walk(child);
  }

  for (const child of fragment.childNodes) walk(child);
  return { text, attachments };
}

function getInlineSelectionContainer() {
  const sel = window.getSelection();
  if (!sel?.anchorNode) return null;
  const anchor = sel.anchorNode;
  return (anchor.nodeType === Node.ELEMENT_NODE ? anchor : anchor.parentElement)?.closest(
    ".msg-inline-body, .msg .body"
  );
}

function getInlineSelectionPayload() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  if (!getInlineSelectionContainer()) return null;
  const range = sel.getRangeAt(0);
  const fragment = range.cloneContents();
  const { text, attachments } = fragmentToPlainAndAttachments(fragment);
  const htmlWrap = document.createElement("div");
  htmlWrap.appendChild(range.cloneContents());
  return { text, attachments, html: htmlWrap.innerHTML };
}

function handleInlineCopy(e) {
  const payload = getInlineSelectionPayload();
  if (!payload) return;

  e.preventDefault();
  e.clipboardData.setData("text/plain", payload.text);
  if (payload.attachments.length) {
    e.clipboardData.setData(YOYA_CLIPBOARD, JSON.stringify(payload.attachments));
  }
  e.clipboardData.setData("text/html", payload.html);
}

async function copyCurrentSelection() {
  const payload = getInlineSelectionPayload();
  if (!payload || (!payload.text && !payload.attachments.length)) return false;
  try {
    const items = {
      "text/plain": new Blob([payload.text], { type: "text/plain" }),
      "text/html": new Blob([payload.html], { type: "text/html" }),
    };
    if (payload.attachments.length) {
      items[YOYA_CLIPBOARD] = new Blob([JSON.stringify(payload.attachments)], {
        type: "application/json",
      });
    }
    await navigator.clipboard.write([new ClipboardItem(items)]);
    return true;
  } catch {
    try {
      await navigator.clipboard.writeText(payload.text);
      return true;
    } catch {
      return false;
    }
  }
}

function clipboardDataFromPayload(payload) {
  return {
    getData(type) {
      if (type === "text/plain") return payload?.text || "";
      if (type === "text/html") return payload?.html || "";
      if (type === YOYA_CLIPBOARD) return payload?.custom || "";
      return "";
    },
    files: [],
    items: [],
  };
}

async function pasteClipboardToComposer() {
  if (isActiveBusy() || !mainComposer) return;
  mainComposer.focus();

  if (await pasteFromNativeClipboard()) return;

  try {
    const items = await navigator.clipboard.read();
    for (const item of items) {
      if (item.types.includes(YOYA_CLIPBOARD)) {
        const customRaw = await (await item.getType(YOYA_CLIPBOARD)).text();
        let attachments = [];
        try {
          attachments = JSON.parse(customRaw);
        } catch {
          attachments = [];
        }
        const plain = item.types.includes("text/plain")
          ? await (await item.getType("text/plain")).text()
          : "";
        mainComposer.importPlainWithAttachments(plain, attachments);
        return;
      }
      for (const type of item.types) {
        if (!type.startsWith("image/")) continue;
        const blob = await item.getType(type);
        const ext = type.split("/")[1] || "png";
        mainComposer.addFile(new File([blob], `clipboard.${ext}`, { type }));
        return;
      }
    }
    const text = await navigator.clipboard.readText();
    if (text) {
      mainComposer.insertText(text);
      return;
    }
    showToast("无法粘贴剪贴板内容", "error");
  } catch {
    try {
      const text = await navigator.clipboard.readText();
      if (text) mainComposer.insertText(text);
      else showToast("无法粘贴剪贴板内容", "error");
    } catch {
      showToast("无法粘贴剪贴板内容", "error");
    }
  }
}

async function applyComposerPaste(cd) {
  if (!cd || !mainComposer) return false;
  if (mainComposer.handlePaste(cd)) return true;

  const customRaw = cd.getData(YOYA_CLIPBOARD);
  if (customRaw) {
    let attachments = [];
    try {
      attachments = JSON.parse(customRaw);
    } catch {
      attachments = [];
    }
    const plain = cd.getData("text/plain") || "";
    mainComposer.importPlainWithAttachments(plain, attachments);
    return !!(plain || attachments.length);
  }

  const text = cd.getData("text/plain");
  if (text) {
    mainComposer.insertText(text);
    return true;
  }
  return false;
}

function handleComposerPaste(e) {
  if (e.defaultPrevented || !mainComposer) return;
  if (mainComposer.handlePaste(e.clipboardData)) e.preventDefault();
}

async function pasteFromNativeClipboard() {
  const api = window.pywebview?.api;
  if (!api?.read_clipboard) return false;
  try {
    const payload = await api.read_clipboard();
    if (!payload) return false;
    const hasContent = !!(payload.text || payload.html || payload.custom);
    if (!hasContent) return false;
    return applyComposerPaste(clipboardDataFromPayload(payload));
  } catch {
    return false;
  }
}

function hideContextMenu() {
  $("context-menu")?.classList.add("hidden");
  state.contextMenuFromSelection = false;
}

function resolveContextMenuZoneFromSelection() {
  const container = getInlineSelectionContainer();
  if (container?.closest(".msg")) return "messages";
  const active = document.activeElement;
  if (active?.id === "message-input" || active?.closest("#composer-wrap")) return "composer";
  return null;
}

function getSelectionMenuPosition() {
  const sel = window.getSelection();
  if (!sel?.rangeCount || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  const rect = range.getBoundingClientRect();
  if (!rect.width && !rect.height) return null;
  return {
    x: Math.min(rect.right, window.innerWidth - 8),
    y: rect.bottom + 6,
  };
}

function maybeShowSelectionContextMenu() {
  if (state.suppressSelectionContextMenuUntil > Date.now()) return;

  const payload = getInlineSelectionPayload();
  if (!payload || (!payload.text && !payload.attachments.length)) {
    if (state.contextMenuFromSelection) hideContextMenu();
    return;
  }

  const zone = resolveContextMenuZoneFromSelection();
  if (!zone) {
    if (state.contextMenuFromSelection) hideContextMenu();
    return;
  }

  const pos = getSelectionMenuPosition();
  if (!pos) return;

  state.contextMenuFromSelection = true;
  state.ignoreContextMenuDismissUntil = Date.now() + 280;
  showContextMenu(pos.x, pos.y, zone);
}

function showContextMenu(x, y, zone) {
  const menu = $("context-menu");
  if (!menu) return;

  const payload = getInlineSelectionPayload();
  const canCopy = !!(payload && (payload.text || payload.attachments.length));
  const canPaste = zone === "composer" && !isActiveBusy();

  let canDiscuss = false;
  if (canCopy && zone === "messages") {
    const sel = window.getSelection();
    const root = sel?.anchorNode;
    const msgEl = (root?.nodeType === Node.ELEMENT_NODE ? root : root?.parentElement)?.closest(".msg");
    canDiscuss = !!(msgEl && $("messages").contains(msgEl));
  }

  const copyBtn = menu.querySelector('[data-action="copy"]');
  const pasteBtn = menu.querySelector('[data-action="paste"]');
  const discussBtn = menu.querySelector('[data-action="discuss"]');
  copyBtn.disabled = !canCopy;
  pasteBtn.disabled = !canPaste;
  discussBtn.disabled = !canDiscuss;
  pasteBtn.classList.toggle("hidden", zone !== "composer");
  discussBtn.classList.toggle("hidden", zone !== "messages");

  menu.classList.remove("hidden");
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;

  const rect = menu.getBoundingClientRect();
  let left = x;
  let top = y;
  if (left + rect.width > window.innerWidth - 8) left = window.innerWidth - rect.width - 8;
  if (top + rect.height > window.innerHeight - 8) top = window.innerHeight - rect.height - 8;
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
}

function bindContextMenu() {
  const menu = $("context-menu");
  if (!menu) return;

  const selectionZones = [
    { id: "messages-wrap", zone: "messages" },
    { id: "composer-wrap", zone: "composer" },
  ];

  for (const { id, zone } of selectionZones) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      state.suppressSelectionContextMenuUntil = Date.now() + 400;
      state.contextMenuFromSelection = false;
      showContextMenu(e.clientX, e.clientY, zone);
    });
    el.addEventListener("mouseup", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest("#context-menu")) return;
      window.setTimeout(() => maybeShowSelectionContextMenu(), 0);
    });
  }

  document.addEventListener("keyup", () => {
    window.setTimeout(() => maybeShowSelectionContextMenu(), 0);
  });

  let selectionCollapseTimer = 0;
  document.addEventListener("selectionchange", () => {
    window.clearTimeout(selectionCollapseTimer);
    selectionCollapseTimer = window.setTimeout(() => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) {
        if (state.contextMenuFromSelection) hideContextMenu();
      }
    }, 80);
  });

  menu.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn || btn.disabled) return;
    const action = btn.dataset.action;
    hideContextMenu();
    state.suppressSelectionContextMenuUntil = Date.now() + 500;
    if (action === "copy") {
      const ok = await copyCurrentSelection();
      window.getSelection()?.removeAllRanges();
      if (!ok) showToast("无法复制选中内容", "error");
    } else if (action === "paste") {
      await pasteClipboardToComposer();
    } else if (action === "discuss") {
      startDiscussionFromSelection();
    }
  });

  document.addEventListener("mousedown", (e) => {
    if (e.target.closest("#context-menu")) return;
    if (Date.now() < state.ignoreContextMenuDismissUntil) return;
    hideContextMenu();
  });
  document.addEventListener("scroll", hideContextMenu, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideContextMenu();
  });
}

function addPathAttachments(items) {
  if (!state.activeAgentId || !items?.length) return;
  mainComposer?.addPathAttachments(items);
}

async function resolveDropPaths(dataTransfer) {
  const api = window.pywebview?.api;
  if (api?.consume_dropped_paths) {
    const native = await api.consume_dropped_paths();
    if (native?.length) return native;
  }
  const fromFiles = [];
  for (const file of dataTransfer?.files || []) {
    const p = file.path || file.pywebviewFullPath;
    if (p) {
      fromFiles.push({
        name: file.name,
        path: p,
        kind: file.type === "" ? "directory" : "file",
      });
    }
  }
  return fromFiles;
}

function clearDropHighlight() {
  $("composer-wrap")?.classList.remove("drag-over");
  $("messages-wrap")?.classList.remove("drag-over");
}

async function handlePathDrop(e) {
  e.preventDefault();
  clearDropHighlight();
  const paths = await resolveDropPaths(e.dataTransfer);
  if (paths.length) {
    addPathAttachments(paths);
    return;
  }
  const files = [...(e.dataTransfer?.files || [])];
  if (files.length) {
    for (const file of files) mainComposer?.addFile(file);
    return;
  }
  showToast("请拖入本机文件或文件夹以引用路径", "error");
}

function bindDropZone(el) {
  if (!el) return;
  el.addEventListener("dragover", (e) => {
    e.preventDefault();
    el.classList.add("drag-over");
  });
  el.addEventListener("dragleave", (e) => {
    if (!el.contains(e.relatedTarget)) el.classList.remove("drag-over");
  });
  el.addEventListener("drop", handlePathDrop);
}

function openAgentDrawer(agentId) {
  const id = agentId || state.activeAgentId;
  if (!id) return;
  if (id !== state.activeAgentId) {
    state.openDrawerAfterSelect = true;
    selectAgent(id);
    return;
  }
  showAgentDrawer();
}

function showAgentDrawer() {
  if (!state.activeAgentId) return;
  $("agent-drawer").classList.add("open");
  if (state.activeAgent) populateAgentDrawer(state.activeAgent);
  requestModelsIfNeeded();
  loadAgentSoul();
}

function closeAgentDrawer() {
  $("agent-drawer").classList.remove("open");
}

function saveAgentConfig() {
  if (!state.activeAgentId || state.savingAgent) return;
  if (!ensureConnected("无法保存：尚未连接")) return;
  const cwd = $("agent-cwd-input").value.trim();
  if (!cwd) {
    showToast("请选择工作目录", "error");
    return;
  }
  state.savingAgent = true;
  setDrawerSaveStatus("保存中…");
  const btn = $("btn-save-agent");
  btn.disabled = true;
  btn.textContent = "保存中…";
  if (!send({
    type: "update_agent",
    agentId: state.activeAgentId,
    name: $("agent-name-input").value.trim() || "Agent",
    cwd,
    model: $("agent-model-input").value.trim() || state.defaultModel,
    enableSoul: $("toggle-soul").checked,
    enableRules: $("toggle-rules").checked,
    enableSkills: $("toggle-skills").checked,
    enableMemory: $("toggle-memory").checked,
    rulesDir: $("rules-dir-input").value.trim(),
    skillsDir: $("skills-dir-input").value.trim(),
    memoryDir: $("memory-dir-input").value.trim(),
    soul: $("soul-editor").value,
  })) {
    state.savingAgent = false;
    resetSaveAgentButton();
    setDrawerSaveStatus("");
    showToast("保存失败：连接已断开", "error");
  }
}

function saveSoul() {
  saveAgentConfig();
}

function toggleDiscussRail(open) {
  state.discussRailOpen = open ?? !state.discussRailOpen;
  $("discuss-rail").classList.toggle("collapsed", !state.discussRailOpen);
}

function discussionPanelTitle(d) {
  const anchor = d.anchor || {};
  let text = String(anchor.quoteContent || anchor.quote || "")
    .replace(/\[\[πattach:(.*?)\]\]/g, (_, path) => {
      const parts = String(path).replace(/\/+$/, "").split("/");
      return parts.pop() || path;
    })
    .replace(/\s+/g, " ")
    .trim();
  if (text && text !== "(引用路径)") {
    return text.length > 72 ? `${text.slice(0, 72)}…` : text;
  }
  const attachments = anchor.attachments || [];
  if (attachments.length) {
    return attachments.map((a) => formatAttachmentLabel(a)).join(" · ");
  }
  const firstUser = (d.messages || []).find((m) => m.role === "user");
  if (firstUser?.content) {
    const content = String(firstUser.content).trim();
    return content.length > 72 ? `${content.slice(0, 72)}…` : content;
  }
  return "讨论";
}

function getSummaryRuntime(discussionId) {
  if (!state.summaryRuntimes[discussionId]) {
    state.summaryRuntimes[discussionId] = { busy: false, streamingText: "" };
  }
  return state.summaryRuntimes[discussionId];
}

function setSummaryRuntime(discussionId, patch) {
  const rt = getSummaryRuntime(discussionId);
  Object.assign(rt, patch);
}

function isSummarySectionVisible(discussionId) {
  const d = state.discussions.find((x) => x.id === discussionId);
  const rt = getSummaryRuntime(discussionId);
  if (rt.busy) return true;
  if (state.summarySectionOpen.has(discussionId)) return true;
  if (d?.summary && isActionableSummary(d.summary)) return true;
  return false;
}

function openSummarySection(discussionId) {
  state.summarySectionOpen.add(discussionId);
  syncSummarySectionVisibility(discussionId);
}

function syncSummarySectionVisibility(discussionId) {
  const panel = getDiscussionPanel(discussionId);
  const section = panel?.querySelector(".discussion-summary");
  if (!section) return;
  section.classList.toggle("discussion-summary-hidden", !isSummarySectionVisible(discussionId));
}

function getCombinedSummaryRuntime(combinedId) {
  if (!state.combinedSummaryRuntimes[combinedId]) {
    state.combinedSummaryRuntimes[combinedId] = {
      busy: false,
      streamingText: "",
      phase: null,
      current: 0,
      total: 0,
    };
  }
  return state.combinedSummaryRuntimes[combinedId];
}

function setCombinedSummaryRuntime(combinedId, patch) {
  const rt = getCombinedSummaryRuntime(combinedId);
  Object.assign(rt, patch);
}

function isNoInstructionSummary(text) {
  const t = String(text || "").trim();
  return t === NO_INSTRUCTION_TEXT || NO_INSTRUCTION_LEGACY.has(t);
}

function isActionableSummary(text) {
  const t = String(text || "").trim();
  return t && !isNoInstructionSummary(t);
}

function injectSummaryToComposer(text) {
  if (!isActionableSummary(text) || !mainComposer) return;
  mainComposer.insertText(text.trim());
  mainComposer.focus();
  showToast("已注入主输入框");
}

function updateDiscussToolbar() {
  const count = state.selectedDiscussionIds.size;
  const btn = $("btn-summarize-selected");
  const selectAll = $("discuss-select-all");
  if (btn) {
    btn.disabled = count === 0 || isAnySummaryBusy();
    btn.textContent = count > 0 ? `生成意图 (${count})` : "生成意图";
  }
  if (selectAll) {
    const allSelected =
      state.discussions.length > 0 &&
      state.discussions.every((d) => state.selectedDiscussionIds.has(d.id));
    selectAll.checked = allSelected;
    selectAll.indeterminate =
      count > 0 && !allSelected && count < state.discussions.length;
  }
  for (const panel of document.querySelectorAll(".discussion-panel")) {
    const cb = panel.querySelector(".discussion-select-cb");
    if (cb) cb.checked = state.selectedDiscussionIds.has(panel.dataset.discussionId);
  }
}

function isAnySummaryBusy() {
  return (
    Object.values(state.summaryRuntimes).some((rt) => rt.busy) ||
    Object.values(state.combinedSummaryRuntimes).some((rt) => rt.busy)
  );
}

function toggleDiscussionSelection(discussionId, selected) {
  if (selected) state.selectedDiscussionIds.add(discussionId);
  else state.selectedDiscussionIds.delete(discussionId);
  updateDiscussToolbar();
}

function toggleSelectAllDiscussions(checked) {
  state.selectedDiscussionIds = new Set();
  if (checked) {
    for (const d of state.discussions) state.selectedDiscussionIds.add(d.id);
  }
  updateDiscussToolbar();
}

function requestDiscussionSummary(discussionId, regenerate) {
  const rt = getSummaryRuntime(discussionId);
  if (rt.busy) return;
  const current = state.discussions.find((x) => x.id === discussionId);
  const hasSummary = !!(current?.summary && String(current.summary).trim());
  const shouldRegenerate = regenerate ?? hasSummary;
  openSummarySection(discussionId);
  expandDiscussion(discussionId);
  send({
    type: "summarize_discussion",
    discussionId,
    regenerate: shouldRegenerate,
  });
}

function saveDiscussionSummaryEdit(discussionId, text) {
  const d = state.discussions.find((x) => x.id === discussionId);
  if (!d || (d.summary || "") === text) return;
  d.summary = text;
  send({ type: "update_discussion_summary", discussionId, summary: text });
}

function updateDiscussionSummarizeBtn(discussionId) {
  const panel = getDiscussionPanel(discussionId);
  if (!panel) return;
  const d = state.discussions.find((x) => x.id === discussionId);
  const btn = panel.querySelector(".discussion-panel-summarize");
  if (!btn || !d) return;
  btn.title = d.summary ? "重新生成意图" : "生成意图";
  btn.textContent = d.summary ? "↻" : "∑";
  btn.disabled = getSummaryRuntime(discussionId).busy;
}

function updateDiscussionSummaryDom(discussionId, { busy, text }) {
  const panel = getDiscussionPanel(discussionId);
  if (!panel) return;
  const section = panel.querySelector(".discussion-summary");
  if (!section) return;
  const textarea = section.querySelector(".discussion-summary-editor");
  const injectBtn = section.querySelector(".discussion-summary-inject");
  const summarizeBtn = panel.querySelector(".discussion-panel-summarize");
  if (textarea && document.activeElement !== textarea) {
    textarea.value = text ?? textarea.value;
  } else if (textarea && busy) {
    textarea.value = text ?? "";
  }
  section.classList.toggle("is-summarizing", !!busy);
  section.classList.toggle("is-system-hint", isNoInstructionSummary(textarea?.value));
  if (injectBtn) injectBtn.disabled = busy || !isActionableSummary(textarea?.value);
  if (summarizeBtn) summarizeBtn.disabled = !!busy;
  syncSummarySectionVisibility(discussionId);
  updateDiscussToolbar();
}

function buildDiscussionSummarySection(d) {
  const section = document.createElement("div");
  section.className = "discussion-summary";

  const textarea = document.createElement("textarea");
  textarea.className = "discussion-summary-editor";
  textarea.rows = 4;
  textarea.placeholder = "可在此编辑…";
  textarea.value = d.summary || "";
  textarea.onblur = () => saveDiscussionSummaryEdit(d.id, textarea.value.trim());
  textarea.onclick = (e) => e.stopPropagation();

  const footer = document.createElement("div");
  footer.className = "discussion-summary-footer";

  const injectBtn = document.createElement("button");
  injectBtn.type = "button";
  injectBtn.className = "discussion-summary-inject";
  injectBtn.textContent = "注入主输入框";
  injectBtn.disabled = !isActionableSummary(d.summary);
  injectBtn.onclick = (e) => {
    e.stopPropagation();
    const textareaEl = section.querySelector(".discussion-summary-editor");
    injectSummaryToComposer(textareaEl?.value || d.summary || "");
  };

  footer.appendChild(injectBtn);

  section.appendChild(textarea);
  section.appendChild(footer);
  if (!isSummarySectionVisible(d.id)) {
    section.classList.add("discussion-summary-hidden");
  }
  if (isNoInstructionSummary(d.summary)) {
    section.classList.add("is-system-hint");
  }
  return section;
}

function formatCombinedSummaryTitle(item) {
  const count = (item.discussionIds || []).length;
  const date = item.createdAt ? new Date(item.createdAt).toLocaleString("zh-CN") : "";
  return `${count} 条讨论${date ? ` · ${date}` : ""}`;
}

function combinedSummaryProgressLabel(rt) {
  if (!rt?.busy) return "";
  if (rt.phase === "individual") {
    return `正在逐条提炼 ${rt.current}/${rt.total}…`;
  }
  if (rt.phase === "merge") return "正在合并意图…";
  return "生成中…";
}

function saveCombinedSummaryEdit(combinedId, text) {
  const item = state.combinedSummaries.find((c) => c.id === combinedId);
  if (!item || (item.summary || "") === text) return;
  item.summary = text;
  send({ type: "update_combined_summary", combinedSummaryId: combinedId, summary: text });
}

function updateCombinedSummaryProgress(combinedId, rt) {
  const card = document.querySelector(
    `.combined-summary-card[data-combined-id="${combinedId}"]`
  );
  if (!card) return;
  const progress = card.querySelector(".combined-summary-progress");
  if (progress) progress.textContent = combinedSummaryProgressLabel(rt);
}

function updateCombinedSummaryDom(combinedId, { busy, text, phase, current, total }) {
  const card = document.querySelector(
    `.combined-summary-card[data-combined-id="${combinedId}"]`
  );
  if (!card) return;
  const textarea = card.querySelector(".combined-summary-editor");
  const injectBtn = card.querySelector(".combined-summary-inject");
  const remergeBtn = card.querySelector(".combined-summary-remerge");
  const regenBtn = card.querySelector(".combined-summary-regen");
  const progress = card.querySelector(".combined-summary-progress");
  if (textarea && document.activeElement !== textarea) {
    textarea.value = text ?? textarea.value;
  } else if (textarea && busy) {
    textarea.value = text ?? "";
  }
  card.classList.toggle("is-summarizing", !!busy);
  if (injectBtn) injectBtn.disabled = busy || !isActionableSummary(textarea?.value);
  if (remergeBtn) remergeBtn.disabled = !!busy;
  if (regenBtn) regenBtn.disabled = !!busy;
  if (progress) {
    progress.textContent = busy
      ? combinedSummaryProgressLabel({ busy, phase, current, total })
      : "";
    progress.hidden = !progress.textContent;
  }
  updateDiscussToolbar();
}

function buildCombinedSummaryCard(item) {
  const rt = getCombinedSummaryRuntime(item.id);
  const card = document.createElement("div");
  card.className = "combined-summary-card";
  card.dataset.combinedId = item.id;
  if (rt.busy) card.classList.add("is-summarizing");

  const header = document.createElement("div");
  header.className = "combined-summary-header";

  const title = document.createElement("span");
  title.className = "combined-summary-title";
  title.textContent = formatCombinedSummaryTitle(item);

  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "combined-summary-delete icon-btn";
  deleteBtn.textContent = "×";
  deleteBtn.title = "删除合并意图";
  deleteBtn.onclick = () => {
    send({ type: "delete_combined_summary", combinedSummaryId: item.id });
  };

  header.appendChild(title);
  header.appendChild(deleteBtn);

  const progress = document.createElement("div");
  progress.className = "combined-summary-progress";
  progress.textContent = combinedSummaryProgressLabel(rt);
  progress.hidden = !progress.textContent;

  const textarea = document.createElement("textarea");
  textarea.className = "combined-summary-editor";
  textarea.rows = 5;
  textarea.placeholder = "可在此编辑…";
  textarea.value = rt.busy && rt.streamingText ? rt.streamingText : item.summary || "";
  textarea.onblur = () => saveCombinedSummaryEdit(item.id, textarea.value.trim());

  const footer = document.createElement("div");
  footer.className = "combined-summary-footer";

  const remergeBtn = document.createElement("button");
  remergeBtn.type = "button";
  remergeBtn.className = "combined-summary-remerge";
  remergeBtn.textContent = "重新合并";
  remergeBtn.title = "用当前各讨论的意图草稿重新合并";
  remergeBtn.disabled = rt.busy;
  remergeBtn.onclick = () => {
    send({ type: "resummarize_combined", combinedSummaryId: item.id });
  };

  const regenBtn = document.createElement("button");
  regenBtn.type = "button";
  regenBtn.className = "combined-summary-regen";
  regenBtn.textContent = "全部重生成";
  regenBtn.title = "重新提炼各讨论意图并合并";
  regenBtn.disabled = rt.busy;
  regenBtn.onclick = () => {
    send({
      type: "resummarize_combined",
      combinedSummaryId: item.id,
      regenerateIndividuals: true,
    });
  };

  const injectBtn = document.createElement("button");
  injectBtn.type = "button";
  injectBtn.className = "combined-summary-inject";
  injectBtn.textContent = "注入主输入框";
  injectBtn.disabled = rt.busy || !isActionableSummary(item.summary);
  injectBtn.onclick = () => {
    const textareaEl = card.querySelector(".combined-summary-editor");
    injectSummaryToComposer(textareaEl?.value || item.summary || "");
  };

  footer.appendChild(remergeBtn);
  footer.appendChild(regenBtn);
  footer.appendChild(injectBtn);

  card.appendChild(header);
  card.appendChild(progress);
  card.appendChild(textarea);
  card.appendChild(footer);

  return card;
}

function renderCombinedSummaries() {
  const container = $("combined-summaries");
  if (!container) return;
  container.innerHTML = "";
  if (!state.combinedSummaries.length) return;

  const heading = document.createElement("div");
  heading.className = "combined-summaries-heading";
  heading.textContent = "合并意图";
  container.appendChild(heading);

  for (const item of state.combinedSummaries) {
    container.appendChild(buildCombinedSummaryCard(item));
  }
}

function syncExpandedDiscussionId() {
  if (
    state.expandedDiscussionId &&
    !state.discussions.some((d) => d.id === state.expandedDiscussionId)
  ) {
    state.expandedDiscussionId = null;
    state.maximizedDiscussionId = null;
  }
  if (state.maximizedDiscussionId && state.maximizedDiscussionId !== state.expandedDiscussionId) {
    state.maximizedDiscussionId = null;
  }
  if (!state.expandedDiscussionId) {
    const hasExplicitCollapse = state.discussions.some((d) => d.collapsed === true);
    if (hasExplicitCollapse) {
      const open = state.discussions.find((d) => d.collapsed === false);
      if (open) state.expandedDiscussionId = open.id;
    }
  }
}

function persistDiscussionCollapsed(discussionId, collapsed) {
  if (!discussionId) return;
  const item = state.discussions.find((d) => d.id === discussionId);
  if (item) item.collapsed = collapsed;
  send({ type: "set_discussion_collapsed", discussionId, collapsed });
}

function setExpandedDiscussion(discussionId) {
  if (!discussionId) return;
  if (state.expandedDiscussionId === discussionId) {
    state.expandedDiscussionId = null;
    state.maximizedDiscussionId = null;
    persistDiscussionCollapsed(discussionId, true);
  } else {
    if (state.expandedDiscussionId) persistDiscussionCollapsed(state.expandedDiscussionId, true);
    state.expandedDiscussionId = discussionId;
    state.maximizedDiscussionId = null;
    persistDiscussionCollapsed(discussionId, false);
  }
  syncDiscussionPanelStates();
}

function expandDiscussion(discussionId) {
  if (!discussionId || state.expandedDiscussionId === discussionId) return;
  if (state.expandedDiscussionId) persistDiscussionCollapsed(state.expandedDiscussionId, true);
  state.expandedDiscussionId = discussionId;
  state.maximizedDiscussionId = null;
  persistDiscussionCollapsed(discussionId, false);
  syncDiscussionPanelStates();
}

function toggleDiscussionMaximize(discussionId) {
  if (!discussionId) return;
  if (state.expandedDiscussionId !== discussionId) {
    state.expandedDiscussionId = discussionId;
  }
  state.maximizedDiscussionId =
    state.maximizedDiscussionId === discussionId ? null : discussionId;
  syncDiscussionPanelStates();
  scrollDiscussionPanelsToBottom();
}

function syncDiscussionPanelStates() {
  const rail = $("discuss-rail");
  if (rail) {
    rail.classList.toggle("discussion-maximized", !!state.maximizedDiscussionId);
  }
  for (const panel of document.querySelectorAll(".discussion-panel")) {
    const id = panel.dataset.discussionId;
    const expanded = id === state.expandedDiscussionId;
    panel.classList.toggle("expanded", expanded);
    panel.classList.toggle("collapsed", !expanded);
    panel.classList.toggle("maximized", id === state.maximizedDiscussionId);
    const titleBtn = panel.querySelector(".discussion-panel-title");
    if (titleBtn) {
      titleBtn.title = expanded ? "点击折叠" : "点击展开";
    }
    const maxBtn = panel.querySelector(".discussion-panel-maximize");
    if (maxBtn) {
      const isMax = id === state.maximizedDiscussionId;
      maxBtn.textContent = isMax ? "⤡" : "⤢";
      maxBtn.title = isMax ? "退出放大" : "放大铺满";
      maxBtn.classList.toggle("active", isMax);
    }
  }
}

function buildDiscussionPanelHeader(d, drt) {
  const header = document.createElement("div");
  header.className = "discussion-panel-header";

  const selectCb = document.createElement("input");
  selectCb.type = "checkbox";
  selectCb.className = "discussion-select-cb";
  selectCb.checked = state.selectedDiscussionIds.has(d.id);
  selectCb.title = "选中以合并生成意图";
  selectCb.onclick = (e) => {
    e.stopPropagation();
    toggleDiscussionSelection(d.id, selectCb.checked);
  };

  const summarizeBtn = document.createElement("button");
  summarizeBtn.type = "button";
  summarizeBtn.className = "discussion-panel-summarize icon-btn";
  summarizeBtn.title = d.summary ? "重新生成意图" : "生成意图";
  summarizeBtn.textContent = d.summary ? "↻" : "∑";
  summarizeBtn.disabled = getSummaryRuntime(d.id).busy;
  summarizeBtn.onclick = (e) => {
    e.stopPropagation();
    requestDiscussionSummary(d.id);
  };

  const titleBtn = document.createElement("button");
  titleBtn.type = "button";
  titleBtn.className = "discussion-panel-title";
  titleBtn.textContent = discussionPanelTitle(d);
  if (drt?.busy) {
    titleBtn.classList.add("is-busy");
    titleBtn.dataset.busyLabel = discussionPanelTitle(d);
  }
  titleBtn.onclick = () => setExpandedDiscussion(d.id);
  titleBtn.title = state.expandedDiscussionId === d.id ? "点击折叠" : "点击展开";

  const actions = document.createElement("div");
  actions.className = "discussion-panel-actions";

  const locateBtn = document.createElement("button");
  locateBtn.type = "button";
  locateBtn.className = "discussion-panel-locate icon-btn";
  locateBtn.title = "定位到引用";
  locateBtn.textContent = "↗";
  locateBtn.onclick = (e) => {
    e.stopPropagation();
    scrollToAnchor(d.anchor, d.id);
  };

  const maxBtn = document.createElement("button");
  maxBtn.type = "button";
  maxBtn.className = "discussion-panel-maximize icon-btn";
  maxBtn.title = "放大铺满";
  maxBtn.textContent = "⤢";
  maxBtn.onclick = (e) => {
    e.stopPropagation();
    toggleDiscussionMaximize(d.id);
  };

  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "discussion-panel-delete icon-btn";
  deleteBtn.title = "删除讨论";
  deleteBtn.textContent = "×";
  deleteBtn.onclick = (e) => {
    e.stopPropagation();
    deleteDiscussion(d.id);
  };

  actions.appendChild(locateBtn);
  actions.appendChild(maxBtn);
  actions.appendChild(deleteBtn);
  header.appendChild(selectCb);
  header.appendChild(summarizeBtn);
  header.appendChild(titleBtn);
  header.appendChild(actions);
  return header;
}

function buildDiscussionPanel(d) {
  const panel = document.createElement("div");
  panel.className = "discussion-panel";
  panel.dataset.discussionId = d.id;
  const drt = getDiscussionRuntime(d.id);

  panel.appendChild(buildDiscussionPanelHeader(d, drt));

  const body = document.createElement("div");
  body.className = "discussion-panel-body";
  body.appendChild(buildDiscussionAnchorEl(d.anchor, d.id));

  const msgBox = document.createElement("div");
  msgBox.className = "discussion-messages";
  for (const m of d.messages || []) {
    msgBox.appendChild(buildDiscussionMsgEl(m));
  }
  body.appendChild(msgBox);

  const composer = document.createElement("div");
  composer.className = "discussion-composer";
  composer.innerHTML = `
      <input type="text" placeholder="追问…" class="discussion-input" />
      <button type="button" class="discussion-send">发送</button>`;
  body.appendChild(composer);

  const input = composer.querySelector(".discussion-input");
  const sendBtn = composer.querySelector(".discussion-send");
  if (drt.busy) {
    input.disabled = true;
    sendBtn.disabled = true;
  }
  const doSend = () => {
    const text = input.value.trim();
    if (!text) return;
    if (!ensureConnected("讨论未发送：尚未连接到服务")) return;
    if (!send({ type: "discussion_send", discussionId: d.id, message: text })) {
      showToast("讨论未发送：连接已断开", "error");
      return;
    }
    input.value = "";
    const runtime = getDiscussionRuntime(d.id);
    runtime.busy = true;
    updateDiscussionTitleBusy(d.id, true);
    sendBtn.disabled = true;
    input.disabled = true;
  };
  sendBtn.onclick = doSend;
  input.onkeydown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      doSend();
    }
  };

  panel.appendChild(body);
  panel.appendChild(buildDiscussionSummarySection(d));
  return panel;
}

function renderDiscussions() {
  const container = $("discussion-panels");
  container.innerHTML = "";
  if (!state.discussions.length) {
    state.expandedDiscussionId = null;
    state.maximizedDiscussionId = null;
    state.selectedDiscussionIds = new Set();
    container.innerHTML = '<p class="discuss-empty">选中主对话文字后点击「讨论」</p>';
    $("discuss-rail")?.classList.remove("discussion-maximized");
    updateDiscussToolbar();
    return;
  }

  syncExpandedDiscussionId();

  for (const d of state.discussions) {
    const panel = buildDiscussionPanel(d);
    container.appendChild(panel);
    const drt = getDiscussionRuntime(d.id);
    if (drt.busy) {
      updateDiscussionStreaming(d.id, drt);
    }
    const srt = getSummaryRuntime(d.id);
    if (srt.busy) {
      updateDiscussionSummaryDom(d.id, { busy: true, text: srt.streamingText });
    }
  }
  syncDiscussionPanelStates();
  scrollDiscussionPanelsToBottom();
  updateDiscussToolbar();
}

function applyDiscussionStream(discussionId, msg) {
  const drt = getDiscussionRuntime(discussionId);
  drt.busy = true;
  AgentFSM.applyStreamPayload(drt, msg);
  drt.streamingText = drt.text || "";
  updateDiscussionTitleBusy(discussionId, true);
  updateDiscussionStreaming(discussionId, drt);
}

function buildDiscussionMsgEl(m) {
  const el = document.createElement("div");
  el.className = `discussion-msg ${m.role}`;
  if (m.role === "assistant" && (m.segments?.length || m.blocks?.length || m.content)) {
    const body = document.createElement("div");
    body.className = "discussion-msg-body";
    renderAssistantBody(body, m);
    el.appendChild(body);
    return el;
  }
  const inlineBody = document.createElement("div");
  inlineBody.className = "msg-inline-body discussion-inline-body";
  renderInlineBody(inlineBody, m.content, m.attachments);
  el.appendChild(inlineBody);
  return el;
}

function appendDiscussionMessage(discussionId, message) {
  const d = state.discussions.find((x) => x.id === discussionId);
  if (d) {
    if (!d.messages) d.messages = [];
    d.messages.push(message);
  }
}

function getDiscussionPanel(discussionId) {
  return document.querySelector(`.discussion-panel[data-discussion-id="${discussionId}"]`);
}

function appendDiscussionUserMessageDom(discussionId, message) {
  const panel = getDiscussionPanel(discussionId);
  if (!panel) return false;
  const msgBox = panel.querySelector(".discussion-messages");
  if (!msgBox) return false;
  panel.querySelector(".discussion-msg.streaming")?.remove();
  msgBox.appendChild(buildDiscussionMsgEl(message));
  msgBox.scrollTop = msgBox.scrollHeight;
  return true;
}

function finalizeDiscussionPanel(discussionId, message) {
  const panel = getDiscussionPanel(discussionId);
  if (!panel) return false;
  const msgBox = panel.querySelector(".discussion-messages");
  if (!msgBox) return false;
  panel.querySelector(".discussion-msg.streaming")?.remove();
  if (message) {
    msgBox.appendChild(buildDiscussionMsgEl(message));
  }
  msgBox.scrollTop = msgBox.scrollHeight;
  updateDiscussionTitleBusy(discussionId, false);
  return true;
}

function updateDiscussionTitleBusy(discussionId, busy) {
  const panel = getDiscussionPanel(discussionId);
  if (!panel) return;
  const titleBtn = panel.querySelector(".discussion-panel-title");
  if (!titleBtn) return;
  const label = titleBtn.dataset.busyLabel || titleBtn.textContent.replace(/^●\s*/, "");
  titleBtn.dataset.busyLabel = label;
  titleBtn.classList.toggle("is-busy", busy);
  titleBtn.textContent = busy ? `● ${label}` : label;
  const input = panel.querySelector(".discussion-input");
  const sendBtn = panel.querySelector(".discussion-send");
  if (input) input.disabled = !!busy;
  if (sendBtn) sendBtn.disabled = !!busy;
}

function updateDiscussionStreaming(discussionId, drt) {
  const panel = getDiscussionPanel(discussionId);
  if (!panel) return;
  let streamEl = panel.querySelector(".discussion-msg.streaming");
  if (!streamEl) {
    streamEl = document.createElement("div");
    streamEl.className = "discussion-msg assistant streaming";
    panel.querySelector(".discussion-messages").appendChild(streamEl);
  }
  let body = streamEl.querySelector(".discussion-msg-body");
  if (!body) {
    body = document.createElement("div");
    body.className = "discussion-msg-body";
    streamEl.appendChild(body);
  }
  patchAssistantBody(
    body,
    { segments: drt.segments || [] },
    { streaming: true, activityText: drt.activityText || "" }
  );
  const msgBox = panel.querySelector(".discussion-messages");
  msgBox.scrollTop = msgBox.scrollHeight;
}

function unwrapHighlightMark(mark) {
  const parent = mark?.parentNode;
  if (!parent) return;
  while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
  parent.removeChild(mark);
  parent.normalize();
}

function clearAnchorHighlights() {
  for (const mark of document.querySelectorAll(".anchor-quote-highlight")) {
    unwrapHighlightMark(mark);
  }
}

function removeDiscussionHighlight(discussionId) {
  if (!discussionId) return;
  const mark = document.querySelector(
    `.anchor-quote-highlight[data-discussion-id="${discussionId}"]`
  );
  if (mark) unwrapHighlightMark(mark);
}

function quoteSearchCandidates(quoteText, anchor) {
  const candidates = [];
  const add = (value) => {
    const text = String(value || "").trim();
    if (text && !candidates.includes(text)) candidates.push(text);
  };

  add(quoteText);
  for (const line of String(quoteText || "").split("\n")) {
    add(line.trim());
  }
  for (const item of anchor?.attachments || []) {
    add(attachmentDisplayPath(item));
    add(formatAttachmentLabel(item));
  }
  const markerPaths = String(anchor?.quoteContent || "").match(/\[\[πattach:(.*?)\]\]/g) || [];
  for (const raw of markerPaths) {
    const path = raw.replace(/^\[\[πattach:(.*)\]\]$/, "$1");
    add(path);
    add(formatAttachmentLabel({ path }));
  }
  return candidates.filter((text) => text.length >= 2);
}

function textOffsetWithin(root, node, offset) {
  if (!root || !node) return -1;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.textContent) return NodeFilter.FILTER_REJECT;
      if (n.parentElement?.closest(".anchor-quote-highlight")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  let acc = 0;
  let n;
  while ((n = walker.nextNode())) {
    if (n === node) return acc + Math.min(offset, n.textContent.length);
    acc += n.textContent.length;
  }
  return -1;
}

function wrapFirstTextMatch(root, search, discussionId, offset) {
  if (!search || !root) return null;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.textContent) return NodeFilter.FILTER_REJECT;
      if (node.parentElement?.closest(".anchor-quote-highlight")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });

  const nodes = [];
  let full = "";
  let n;
  while ((n = walker.nextNode())) {
    nodes.push({ node: n, start: full.length });
    full += n.textContent;
  }

  let idx = -1;
  const hasOffset = Number.isFinite(offset) && offset >= 0;
  const startFrom = hasOffset ? offset : 0;
  idx = full.indexOf(search, startFrom);
  if (idx < 0) return null;
  const endIdx = idx + search.length;

  let startNode = null;
  let startOffset = 0;
  let endNode = null;
  let endOffset = 0;
  for (const item of nodes) {
    const nodeEnd = item.start + item.node.textContent.length;
    if (!startNode && idx >= item.start && idx < nodeEnd) {
      startNode = item.node;
      startOffset = idx - item.start;
    }
    if (endIdx > item.start && endIdx <= nodeEnd) {
      endNode = item.node;
      endOffset = endIdx - item.start;
      break;
    }
  }
  if (!startNode || !endNode) return null;

  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);

  const mark = document.createElement("mark");
  mark.className = "anchor-quote-highlight";
  if (discussionId) mark.dataset.discussionId = discussionId;
  try {
    range.surroundContents(mark);
  } catch {
    mark.appendChild(range.extractContents());
    range.insertNode(mark);
  }
  return mark;
}

function highlightQuoteInElement(root, anchor, discussionId) {
  const quote = String(anchor?.quote || "").trim();
  if (!root || !quote) return null;
  const offset = Number(anchor?.offset);
  const startFrom = Number.isFinite(offset) ? offset : -1;
  for (const candidate of quoteSearchCandidates(quote, anchor)) {
    const found = wrapFirstTextMatch(root, candidate, discussionId, startFrom);
    if (found) return found;
  }
  return null;
}

function applyDiscussionAnchorHighlight(anchor, discussionId) {
  if (!anchor) return null;
  removeDiscussionHighlight(discussionId);

  const idx = Number(anchor.messageIndex);
  if (!Number.isFinite(idx)) return null;

  const msgEl = document.querySelector(`.msg[data-message-index="${idx}"]`);
  if (!msgEl) return null;

  const body = msgEl.querySelector(".body");
  return highlightQuoteInElement(body, anchor, discussionId);
}

function refreshAllDiscussionHighlights() {
  clearAnchorHighlights();
  for (const d of state.discussions) {
    if (d.agentId === state.activeAgentId && d.anchor) {
      applyDiscussionAnchorHighlight(d.anchor, d.id);
    }
  }
}

function scrollToAnchor(anchor, discussionId) {
  if (!anchor) return;

  let highlightEl = discussionId
    ? document.querySelector(`.anchor-quote-highlight[data-discussion-id="${discussionId}"]`)
    : null;
  if (!highlightEl) {
    highlightEl = applyDiscussionAnchorHighlight(anchor, discussionId);
  }

  const idx = Number(anchor.messageIndex);
  const msgEl = Number.isFinite(idx)
    ? document.querySelector(`.msg[data-message-index="${idx}"]`)
    : null;
  const scrollTarget = highlightEl || msgEl;
  if (!scrollTarget) return;

  scrollTarget.scrollIntoView({ behavior: "smooth", block: "center" });
}

function resolveMessageIndexFromSelection(container) {
  const msgEl = container.closest?.(".msg");
  if (!msgEl) return 0;
  if (msgEl.dataset.messageIndex != null && !msgEl.classList.contains("streaming")) {
    return Number(msgEl.dataset.messageIndex);
  }
  const indexed = [...$("messages").querySelectorAll(".msg[data-message-index]")];
  if (!indexed.length) return 0;
  return Number(indexed[indexed.length - 1].dataset.messageIndex);
}

function startDiscussionFromSelection() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !state.activeAgentId || !sel.rangeCount) return;

  const range = sel.getRangeAt(0);
  const root = range.commonAncestorContainer;
  const container = root.nodeType === Node.ELEMENT_NODE ? root : root.parentElement;
  if (!container || !$("messages").contains(container)) return;

  const { serialized, plain, attachments } = serializeSelection(sel);
  let quote = plain;
  if ((!quote || quote === "(引用路径)") && attachments.length) {
    quote = attachments
      .map((a) => attachmentDisplayPath(a))
      .filter(Boolean)
      .join("\n");
  }
  if (!quote && attachments.length) {
    quote = attachments
      .map((a) => attachmentDisplayPath(a))
      .filter(Boolean)
      .join("\n");
  }
  if (!quote) return;

  const messageIndex = resolveMessageIndexFromSelection(container);

  const msgEl = container.closest?.(".msg");
  const bodyEl = msgEl?.querySelector(".body") || null;
  let anchorOffset = -1;
  if (bodyEl && range.startContainer?.nodeType === Node.TEXT_NODE) {
    anchorOffset = textOffsetWithin(bodyEl, range.startContainer, range.startOffset);
  }

  const anchor = {
    messageIndex,
    quote,
  };
  if (anchorOffset >= 0) anchor.offset = anchorOffset;
  if (serialized) anchor.quoteContent = serialized;
  if (attachments.length) anchor.attachments = attachments;

  send({
    type: "create_discussion",
    agentId: state.activeAgentId,
    anchor,
  });
  sel.removeAllRanges();
  hideContextMenu();
  toggleDiscussRail(true);
}

function renderModels() {
  const list = $("model-list");
  list.innerHTML = "";
  const current = state.activeAgent?.model || state.defaultModel;
  for (const m of state.models) {
    const el = document.createElement("div");
    el.className = "model-item" + (m.id === current ? " current" : "");
    el.textContent = m.name || m.id;
    el.onclick = () => {
      if (state.activeAgentId) {
        send({
          type: "update_agent",
          agentId: state.activeAgentId,
          model: m.id,
        });
      }
      state.defaultModel = m.id;
      $("model-name").textContent = m.id;
      $("model-modal").classList.remove("open");
      fillModelSelect($("agent-model-input"), m.id);
    };
    list.appendChild(el);
  }
}

function modelCatalog() {
  const byId = new Map();
  for (const item of state.models || []) {
    const id = String(item?.id || "").trim();
    if (!id) continue;
    byId.set(id, { id, name: item.name || id });
  }
  return byId;
}

function fillModelSelect(selectEl, selectedId) {
  if (!selectEl) return;
  const selected = String(selectedId || "").trim() || state.defaultModel || "composer-2.5";
  const byId = modelCatalog();
  if (!byId.has(selected)) byId.set(selected, { id: selected, name: selected });
  if (!byId.has("composer-2.5")) byId.set("composer-2.5", { id: "composer-2.5", name: "composer-2.5" });
  const models = [...byId.values()].sort((a, b) =>
    String(a.name || a.id).localeCompare(String(b.name || b.id), "zh")
  );
  selectEl.innerHTML = "";
  for (const model of models) {
    const opt = document.createElement("option");
    opt.value = model.id;
    opt.textContent = model.name || model.id;
    selectEl.appendChild(opt);
  }
  selectEl.value = selected;
}

function syncModelSelects() {
  fillModelSelect($("setup-model"), $("setup-model")?.value || state.defaultModel);
  fillModelSelect($("system-default-model"), $("system-default-model")?.value || state.defaultModel);
  fillModelSelect(
    $("agent-model-input"),
    $("agent-model-input")?.value || state.activeAgent?.model || state.defaultModel
  );
}

function requestModelsIfNeeded() {
  if (state.models.length || !state.connected || state.needsSetup) return;
  send({ type: "list_models" });
}

function applyFontSize(size) {
  const allowed = new Set(["small", "medium", "large", "xlarge"]);
  const value = allowed.has(size) ? size : "medium";
  document.documentElement.dataset.fontSize = value;
  localStorage.setItem(FONT_SIZE_KEY, value);
  const sel = $("system-font-size");
  if (sel) sel.value = value;
}

function loadFontSizePreference() {
  applyFontSize(localStorage.getItem(FONT_SIZE_KEY) || "medium");
}

function openSystemSettings() {
  $("system-default-cwd").value = state.defaultCwd || "";
  fillModelSelect($("system-default-model"), state.defaultModel || "composer-2.5");
  $("system-api-key").value = "";
  $("system-settings-error").textContent = "";
  $("system-font-size").value = localStorage.getItem(FONT_SIZE_KEY) || "medium";
  $("system-version").textContent = state.version ? `yoya v${state.version}` : "yoya";
  $("system-settings-modal").classList.add("open");
  requestModelsIfNeeded();
}

function closeSystemSettings() {
  $("system-settings-modal").classList.remove("open");
}

async function saveSystemSettings() {
  const errEl = $("system-settings-error");
  errEl.textContent = "";
  applyFontSize($("system-font-size").value);

  const apiKey = $("system-api-key").value.trim();
  const defaultCwd = $("system-default-cwd").value.trim();
  const defaultModel = $("system-default-model").value.trim() || "composer-2.5";

  $("btn-system-settings-save").disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: apiKey,
        default_cwd: defaultCwd,
        default_model: defaultModel,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "保存失败");
    state.defaultCwd = data.defaultCwd || defaultCwd;
    state.defaultModel = data.defaultModel || defaultModel;
    $("setup-cwd").value = state.defaultCwd;
    fillModelSelect($("setup-model"), state.defaultModel);
    closeSystemSettings();
    showToast("系统设置已保存");
  } catch (err) {
    errEl.textContent = err.message || String(err);
  } finally {
    $("btn-system-settings-save").disabled = false;
  }
}

function showSetup(force) {
  $("setup-modal").classList.add("open");
  if (force) $("setup-modal").dataset.required = "1";
}

function hideSetup() {
  $("setup-modal").classList.remove("open");
  $("setup-modal").dataset.required = "0";
}

async function saveSetup() {
  const apiKey = $("setup-api-key").value.trim();
  const defaultCwd = $("setup-cwd").value.trim();
  const defaultModel = $("setup-model").value.trim() || "composer-2.5";
  const errEl = $("setup-error");
  errEl.textContent = "";
  if (!apiKey) {
    errEl.textContent = "请填写 API Key";
    return;
  }
  $("btn-setup-save").disabled = true;
  try {
    const res = await fetch("/api/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey, default_cwd: defaultCwd, default_model: defaultModel }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "保存失败");
    state.needsSetup = false;
    hideSetup();
    location.reload();
  } catch (err) {
    errEl.textContent = err.message || String(err);
  } finally {
    $("btn-setup-save").disabled = false;
  }
}

function bindUi() {
  mainComposer = new AgentComposer({
    rootEl: $("message-input"),
    onUploadingChange: () => updateChrome(),
    onEnter: () => {
      if (!isActiveBusy()) sendMessage();
    },
  });

  $("btn-new-agent").onclick = createAgent;
  $("btn-send").onclick = onComposerAction;
  $("model-badge").onclick = () => $("model-modal").classList.add("open");
  $("model-modal-close").onclick = () => $("model-modal").classList.remove("open");
  $("btn-setup-save").onclick = saveSetup;
  $("btn-system-settings").onclick = openSystemSettings;
  $("system-settings-close").onclick = closeSystemSettings;
  $("btn-system-settings-save").onclick = saveSystemSettings;
  $("system-font-size").onchange = (e) => applyFontSize(e.target.value);
  $("system-settings-modal").addEventListener("click", (e) => {
    if (e.target === $("system-settings-modal")) closeSystemSettings();
  });
  bindPickFolderButton("btn-pick-system-cwd", "system-default-cwd");
  bindClearPathButton("btn-clear-system-cwd", "system-default-cwd");
  $("btn-drawer-close").onclick = closeAgentDrawer;
  $("agent-drawer").addEventListener("click", (e) => {
    if (e.target === $("agent-drawer")) closeAgentDrawer();
  });
  $("btn-save-agent").onclick = saveAgentConfig;
  $("btn-reset-agent").onclick = () => {
    if (!state.activeAgentId) return;
    const agent = state.agents.find((a) => a.id === state.activeAgentId);
    resetAgent(state.activeAgentId, agent?.name || "Agent");
  };
  bindPickFolderButton("btn-pick-cwd", "agent-cwd-input");
  bindClearPathButton("btn-clear-cwd", "agent-cwd-input");
  bindPickFolderButton("btn-pick-rules-dir", "rules-dir-input");
  bindClearPathButton("btn-clear-rules-dir", "rules-dir-input");
  bindPickFolderButton("btn-pick-skills-dir", "skills-dir-input");
  bindClearPathButton("btn-clear-skills-dir", "skills-dir-input");
  bindPickFolderButton("btn-pick-memory-dir", "memory-dir-input");
  bindClearPathButton("btn-clear-memory-dir", "memory-dir-input");
  $("btn-new-rules-file")?.addEventListener("click", () => createConfigFile("rules"));
  $("btn-new-skills-file")?.addEventListener("click", () => createConfigFile("skills"));
  $("btn-new-memory-file")?.addEventListener("click", () => createConfigFile("memory"));
  $("btn-save-config-file")?.addEventListener("click", saveConfigFile);
  $("config-file-body")?.addEventListener("input", () => {
    if (!state.configEditor.path) return;
    state.configEditor.dirty = true;
    syncConfigEditorLabel();
  });
  bindPickFolderButton("btn-pick-setup-cwd", "setup-cwd");
  bindClearPathButton("btn-clear-setup-cwd", "setup-cwd");
  $("btn-discuss-toggle").onclick = () => toggleDiscussRail();
  $("btn-discuss-close").onclick = () => toggleDiscussRail(false);
  $("discuss-select-all")?.addEventListener("change", (e) => {
    toggleSelectAllDiscussions(e.target.checked);
  });
  $("btn-summarize-selected")?.addEventListener("click", () => {
    if (!state.activeAgentId || state.selectedDiscussionIds.size === 0) return;
    if (!ensureConnected()) return;
    send({
      type: "summarize_discussions",
      agentId: state.activeAgentId,
      discussionIds: [...state.selectedDiscussionIds],
    });
  });

  $("setup-modal").addEventListener("click", (e) => {
    if (e.target === $("setup-modal") && $("setup-modal").dataset.required !== "1") hideSetup();
  });

  document.addEventListener("copy", handleInlineCopy);

  $("messages").addEventListener("scroll", () => {
    if (state.suppressScrollAutoUpdate) return;
    state.autoScroll = isNearBottom($("messages"));
    if (state.activeAgentId) saveMessageScrollPosition(state.activeAgentId);
  }, { passive: true });

  window.addEventListener("pagehide", () => {
    if (state.activeAgentId) saveMessageScrollPosition(state.activeAgentId);
  });
  window.addEventListener("beforeunload", () => {
    if (state.activeAgentId) saveMessageScrollPosition(state.activeAgentId);
  });

  bindDropZone($("composer-wrap"));
  bindDropZone($("messages-wrap"));

  bindContextMenu();
  bindLinkViewer();

  window.addEventListener("pywebviewready", () => {
    for (const id of [
      "agent-cwd-input",
      "setup-cwd",
      "system-default-cwd",
      "rules-dir-input",
      "skills-dir-input",
      "memory-dir-input",
    ]) {
      const el = $(id);
      if (el) el.readOnly = true;
    }
    requestAnimationFrame(() => mainComposer?.focus());
  });
}

bindUi();
loadFontSizePreference();
configureMarked();
syncModelSelects();
connect();

function configureMarked() {
  if (!window.marked) return;
  marked.setOptions({
    gfm: true,
    breaks: true,
  });
  marked.use({
    renderer: {
      link(token) {
        const safeHref = escapeHtml(token.href || "");
        const titleAttr = token.title ? ` title="${escapeHtml(token.title)}"` : "";
        let label = token.text || token.href || "";
        try {
          if (token.tokens?.length && this.parser?.parseInline) {
            label = this.parser.parseInline(token.tokens);
          }
        } catch {
          label = token.text || token.href || "";
        }
        return `<a href="${safeHref}" class="msg-link" rel="noopener noreferrer"${titleAttr}>${label}</a>`;
      },
      image(token) {
        const href = token.href || "";
        const alt = token.text || "";
        const { src, display } = resolveMediaUrl(href);
        const label = escapeHtml(display || href || alt || "图片");
        const safeAlt = escapeHtml(alt || display || "图片");
        if (!src) {
          return `<figure class="msg-image msg-image-path-only"><figcaption class="msg-image-path">${label}</figcaption></figure>`;
        }
        const safeSrc = escapeHtml(src);
        return `<figure class="msg-image"><img class="msg-image-media" src="${safeSrc}" alt="${safeAlt}" loading="lazy"><figcaption class="msg-image-path">${label}</figcaption></figure>`;
      },
    },
  });
}

function normalizeLinkHref(raw) {
  const href = String(raw || "").trim();
  if (!href || href.startsWith("#") || href.toLowerCase().startsWith("javascript:")) return "";
  if (/^https?:\/\//i.test(href)) return href;
  if (href.startsWith("//")) return `https:${href}`;
  return href;
}

function linkViewerTitle(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

async function openExternalUrl(url) {
  const api = window.pywebview?.api;
  if (api?.open_external_url) {
    const ok = await api.open_external_url(url);
    if (ok) return true;
  }
  window.open(url, "_blank", "noopener,noreferrer");
  return true;
}

function openLinkViewer(url) {
  const normalized = normalizeLinkHref(url);
  if (!normalized) return;
  if (!/^https?:\/\//i.test(normalized)) {
    openExternalUrl(normalized);
    return;
  }

  state.linkViewerUrl = normalized;
  const viewer = $("link-viewer");
  const frame = $("link-viewer-frame");
  const title = $("link-viewer-title");
  if (!viewer || !frame) return;

  title.textContent = linkViewerTitle(normalized);
  frame.src = normalized;
  viewer.classList.remove("hidden");
  viewer.setAttribute("aria-hidden", "false");
}

function closeLinkViewer() {
  const viewer = $("link-viewer");
  const frame = $("link-viewer-frame");
  if (!viewer) return;
  viewer.classList.add("hidden");
  viewer.setAttribute("aria-hidden", "true");
  if (frame) frame.src = "about:blank";
  state.linkViewerUrl = "";
}

function handleConversationLinkClick(e) {
  const link = e.target.closest("a[href]");
  if (!link) return;
  if (link.closest("#setup-modal, #system-settings-modal, #agent-drawer, #link-viewer")) return;
  if (!link.closest("#messages, #discussion-panels")) return;

  const href = normalizeLinkHref(link.getAttribute("href"));
  if (!href) return;

  e.preventDefault();
  e.stopPropagation();
  openLinkViewer(href);
}

function bindLinkViewer() {
  document.addEventListener("click", handleConversationLinkClick);
  $("link-viewer-back")?.addEventListener("click", closeLinkViewer);
  $("link-viewer-external")?.addEventListener("click", () => {
    if (state.linkViewerUrl) openExternalUrl(state.linkViewerUrl);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("link-viewer")?.classList.contains("hidden")) {
      closeLinkViewer();
    }
  });
}
