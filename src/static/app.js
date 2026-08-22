"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const STEP_KEYS = ["background", "outline", "chapter_outline", "writer", "polisher", "continuity", "archiver"];
const PIPELINE_KEYS = ["background", "background_review", "outline", "review", "chapter_outline", "chapter_review", "writer", "polisher", "continuity", "body_review", "handoff_review"];
const CONFIG_HISTORY_KEY = "novelflow_config_history";

const ROLE_META = {
  default:    { label: "默认配置", icon: "⚙️", desc: "未自定义的角色将继承此配置" },
  background: { label: "背景设计", icon: "🌍", desc: "世界观 · 人物 · 力量体系 · 势力" },
  outline:    { label: "章节细纲", icon: "🗺️", desc: "逐章细纲 · 冲突 · 钩子" },
  chapter_outline: { label: "本轮细纲", icon: "🧭", desc: "当前连续章节的详细规划" },
  writer:     { label: "主笔创作", icon: "✒️", desc: "逐章正文撰写" },
  polisher:   { label: "润色修改", icon: "✨", desc: "语病 · 节奏 · 风格统一" },
  archiver:   { label: "自动归档", icon: "📦", desc: "作品卡 · 简介 · 人物表 · 全书导出" },
  continuity: { label: "连续性记忆", icon: "🧠", desc: "人物状态 · 时间线 · 伏笔" },
};

const STEP_LABEL = {
  background: "背景设计",
  background_review: "背景确认",
  outline: "本卷规划",
  review: "卷规划审核",
  chapter_outline: "本轮细纲",
  chapter_review: "细纲审核",
  writer: "主笔创作",
  polisher: "润色修改",
  archiver: "成果整理",
  continuity: "连续性记忆",
  body_review: "正文审核",
  handoff_review: "卷末交接",
};

const state = {
  settings: null,
  customRoles: new Set(JSON.parse(localStorage.getItem("novelflow_custom_roles") || "[]")),
  workflow: null,
  workflowId: null,
  chapters: [],
  stepOutputs: {},
  chapterBuffer: "",
  currentChapter: 0,
  es: null,
  activeTab: "background",
  configHistory: JSON.parse(localStorage.getItem(CONFIG_HISTORY_KEY) || "[]"),
  liveChars: 0,
};

/* ===================== 工具 ===================== */
function escHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escAttr(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function statusText(st) {
  return { pending: "等待中", running: "创作中", awaiting_background_review: "等待背景确认", awaiting_review: "等待卷规划审核", awaiting_chapter_review: "等待细纲审核", awaiting_body_review: "等待正文审核", awaiting_handoff_review: "等待卷末交接审核", done: "已完成", error: "出错" }[st] || st;
}
function formatTime(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString("zh-CN", { hour12: false }); } catch (_) { return iso; }
}

async function apiGet(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
async function apiPost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ===================== 设置 ===================== */
async function loadSettings() {
  const data = await apiGet("/api/settings");
  state.settings = data.settings || {};
  if (!state.settings.default) {
    state.settings.default = { api_key: "", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini", temperature: 0.8, max_tokens: 4096 };
  }
  renderSettings();
}

function renderSettings() {
  const wrap = $("#settings-list");
  const s = state.settings;
  if (!s) return;
  const defaultCfg = s.default || {};
  let html = "";

  for (const key of ["default", ...STEP_KEYS]) {
    const cfg = s[key] || {};
    const meta = ROLE_META[key];
    const isDefault = key === "default";
    const isCustom = state.customRoles.has(key);
    const eff = isDefault || isCustom ? cfg : defaultCfg;
    const temp = eff.temperature ?? 0.8;
    const roleControl = isDefault
      ? '<div class="role-actions"><button type="button" class="btn mini btn-test-connection" data-role="' + key + '">测试连接</button><button type="button" class="btn mini" id="btn-copy-default">复制到全部角色</button></div>'
      : '<div class="role-actions"><button type="button" class="btn mini btn-test-connection" data-role="' + key + '">测试连接</button><label class="switch"><input type="checkbox" class="role-inherit" data-role="' + key + '" ' + (!isCustom ? "checked" : "") + '><span>跟随默认</span></label></div>';

    html += `
      <div class="role-card ${isDefault ? "role-default" : ""}" data-role="${key}">
        <div class="role-head">
          <div class="role-title">
            <span class="role-icon">${meta.icon}</span>
            <div><strong>${meta.label}</strong><small>${meta.desc}</small></div>
          </div>
          ${roleControl}
        </div>
        <div class="role-fields ${!isDefault && !isCustom ? "is-locked" : ""}">
          <div class="field">
            <label>API Key</label>
            <input type="password" class="cfg-api-key" data-role="${key}" value="${escAttr(eff.api_key || "")}" placeholder="sk-..." autocomplete="off">
          </div>
          <div class="field">
            <label>API 端点 (Base URL)</label>
            <input type="text" class="cfg-base-url" data-role="${key}" value="${escAttr(eff.base_url || "")}" placeholder="https://api.openai.com/v1">
          </div>
          <div class="field">
            <label>模型</label>
            <input type="text" class="cfg-model" data-role="${key}" value="${escAttr(eff.model || "")}" placeholder="gpt-4o-mini">
          </div>
          <div class="field">
            <label>温度 <span class="val" data-role="${key}">${temp}</span></label>
            <input type="range" min="0" max="2" step="0.1" class="cfg-temp" data-role="${key}" value="${temp}">
          </div>
          <div class="field">
            <label>最大输出 Tokens</label>
            <input type="number" class="cfg-max-tokens" data-role="${key}" value="${eff.max_tokens ?? 4096}" min="256" step="256">
          </div>
        </div>
      </div>`;
  }
  wrap.innerHTML = html;
  bindSettingsEvents();
  renderMemoryList();
}

function cloneConfig(value) {
  return JSON.parse(JSON.stringify(value || {}));
}

function rememberSettings(settings) {
  state.configHistory = [{
    id: String(Date.now()),
    savedAt: new Date().toISOString(),
    settings: cloneConfig(settings),
  }, ...state.configHistory].slice(0, 12);
  localStorage.setItem(CONFIG_HISTORY_KEY, JSON.stringify(state.configHistory));
  renderMemoryList();
}

function renderMemoryList() {
  const wrap = $("#settings-history-list");
  if (!wrap) return;
  if (!state.configHistory.length) {
    wrap.innerHTML = '<div class="memory-empty">还没有保存过的配置</div>';
    return;
  }
  wrap.innerHTML = state.configHistory.map((item) => {
    const roles = Object.keys(item.settings || {}).filter((key) => key !== "default").length;
    return `<div class="memory-item" data-memory-id="${escAttr(item.id)}">
      <div><strong>${escHtml(formatTime(item.savedAt))}</strong><small>${roles ? roles + " 个自定义角色" : "默认配置"}</small></div>
      <div class="memory-actions"><button class="btn mini memory-restore" data-memory-id="${escAttr(item.id)}">恢复</button><button class="btn mini ghost memory-delete" data-memory-id="${escAttr(item.id)}">删除</button></div>
    </div>`;
  }).join("");
  wrap.querySelectorAll(".memory-restore").forEach((btn) => btn.addEventListener("click", () => restoreSettings(btn.dataset.memoryId)));
  wrap.querySelectorAll(".memory-delete").forEach((btn) => btn.addEventListener("click", () => deleteSettingsMemory(btn.dataset.memoryId)));
}

function restoreSettings(id) {
  const item = state.configHistory.find((entry) => entry.id === id);
  if (!item) return;
  state.settings = cloneConfig(item.settings);
  state.customRoles = new Set(Object.keys(state.settings).filter((key) => key !== "default"));
  localStorage.setItem("novelflow_custom_roles", JSON.stringify([...state.customRoles]));
  renderSettings();
  showToast("已恢复配置，点击保存后生效");
}

function deleteSettingsMemory(id) {
  state.configHistory = state.configHistory.filter((entry) => entry.id !== id);
  localStorage.setItem(CONFIG_HISTORY_KEY, JSON.stringify(state.configHistory));
  renderMemoryList();
}

function clearSettingsHistory() {
  if (!state.configHistory.length) return;
  state.configHistory = [];
  localStorage.removeItem(CONFIG_HISTORY_KEY);
  renderMemoryList();
  showToast("配置记忆已清空");
}

function bindSettingsEvents() {
  // 跟随默认开关
  $$(".role-inherit").forEach((cb) => {
    cb.addEventListener("change", () => {
      const role = cb.dataset.role;
      if (cb.checked) {
        state.customRoles.delete(role);
      } else {
        state.customRoles.add(role);
        // 从服务器配置（已合并默认）填充到该角色，避免空白
        const src = (state.settings && state.settings[role]) || state.settings.default || {};
        setRoleFields(role, src);
      }
      localStorage.setItem("novelflow_custom_roles", JSON.stringify([...state.customRoles]));
      renderSettings();
    });
  });

  // 温度滑块实时显示数值
  $$(".cfg-temp").forEach((r) => {
    r.addEventListener("input", () => {
      const v = $(".val[data-role='" + r.dataset.role + "']");
      if (v) v.textContent = r.value;
      if (r.dataset.role === "default") syncLockedRoles();
    });
  });

  // 默认配置变化时，同步到「跟随默认」的角色
  $$("[data-role='default']").forEach((el) => {
    if (el.classList.contains("cfg-temp")) return;
    el.addEventListener("input", syncLockedRoles);
  });

  const copyBtn = $("#btn-copy-default");
  if (copyBtn) copyBtn.addEventListener("click", () => {
    const d = readRoleFields("default");
    for (const key of STEP_KEYS) {
      state.customRoles.add(key);
      setRoleFields(key, d);
    }
    localStorage.setItem("novelflow_custom_roles", JSON.stringify([...state.customRoles]));
    renderSettings();
    showToast("已复制默认配置到全部角色");
  });

  $$(".btn-test-connection").forEach((btn) => {
    btn.addEventListener("click", () => testConnection(btn.dataset.role));
  });
}

function setRoleFields(role, cfg) {
  const set = (cls, val) => {
    const el = $("." + cls + "[data-role='" + role + "']");
    if (el) el.value = val;
  };
  set("cfg-api-key", cfg.api_key || "");
  set("cfg-base-url", cfg.base_url || "");
  set("cfg-model", cfg.model || "");
  set("cfg-temp", cfg.temperature ?? 0.8);
  set("cfg-max-tokens", cfg.max_tokens ?? 4096);
  const v = $(".val[data-role='" + role + "']");
  if (v) v.textContent = cfg.temperature ?? 0.8;
}

function syncLockedRoles() {
  const d = readRoleFields("default");
  for (const key of STEP_KEYS) {
    if (!state.customRoles.has(key)) setRoleFields(key, d);
  }
}

function readRoleFields(role) {
  const g = (cls) => $("." + cls + "[data-role='" + role + "']");
  return {
    api_key: (g("cfg-api-key") ? g("cfg-api-key").value : "").trim(),
    base_url: (g("cfg-base-url") ? g("cfg-base-url").value : "").trim(),
    model: (g("cfg-model") ? g("cfg-model").value : "").trim(),
    temperature: parseFloat(g("cfg-temp") ? g("cfg-temp").value : "0.8"),
    max_tokens: parseInt(g("cfg-max-tokens") ? g("cfg-max-tokens").value : "4096", 10) || 4096,
  };
}

function collectSettings() {
  const payload = { default: readRoleFields("default") };
  for (const key of STEP_KEYS) {
    if (state.customRoles.has(key)) payload[key] = readRoleFields(key);
  }
  return payload;
}

function collectSettingsPayload() {
  return {
    settings: collectSettings(),
    inherit_roles: STEP_KEYS.filter((key) => !state.customRoles.has(key)),
  };
}

function openSettings() {
  $("#settings-modal").hidden = false;
  $("#btn-close-settings").focus();
}

function closeSettings() {
  $("#settings-modal").hidden = true;
  $("#btn-settings").focus();
}

async function saveSettings() {
  const btn = $("#btn-save-settings");
  btn.disabled = true;
  btn.textContent = "正在保存...";
  try {
    const payload = collectSettingsPayload();
    await apiPost("/api/settings", payload);
    rememberSettings(payload.settings);
    await loadSettings();
    showToast("AI 配置已保存");
    closeSettings();
  } catch (e) {
    showToast("保存失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "保存配置";
  }
}

function renderApiList() {
  const wrap = $("#api-list");
  if (!wrap) return;
  const settings = state.settings || {};
  wrap.innerHTML = ["default", ...STEP_KEYS].map((role) => {
    const cfg = settings[role] || settings.default || {};
    const endpoint = String(cfg.base_url || "").replace(/\/$/, "") + "/chat/completions";
    return `<div class="api-item"><div class="api-item-head"><strong>${ROLE_META[role]?.icon || "⚙️"} ${ROLE_META[role]?.label || role}</strong><button class="btn mini copy-api" data-api-url="${escAttr(endpoint)}">复制地址</button></div><code>${escHtml(endpoint)}</code><small>模型：${escHtml(cfg.model || "未设置")} · Key：${cfg.api_key ? "已配置" : "未配置"}</small></div>`;
  }).join("");
  wrap.querySelectorAll(".copy-api").forEach((btn) => btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(btn.dataset.apiUrl); showToast("API 地址已复制"); }
    catch (_) { showToast(btn.dataset.apiUrl); }
  }));
}

function openApiViewer() {
  renderApiList();
  $("#api-modal").hidden = false;
  $("#btn-close-api").focus();
}

function closeApiViewer() {
  $("#api-modal").hidden = true;
  $("#btn-api-view").focus();
}

async function testConnection(role) {
  const btn = $(".btn-test-connection[data-role='" + role + "']");
  const config = readRoleFields(role);
  btn.disabled = true;
  btn.textContent = "正在测试...";
  try {
    const effectiveRole = role === "default" || state.customRoles.has(role) ? role : "default";
    const result = await apiPost("/api/test-connection", { config, role: effectiveRole });
    showToast("连接成功：" + (result.reply || result.model));
  } catch (e) {
    showToast("连接失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "测试连接";
  }
}

/* ===================== 启动工作流 ===================== */
function collectInput() {
  return {
    title: $("#in-title").value.trim(),
    genre: $("#in-genre").value.trim() || "玄幻",
    theme: $("#in-theme").value.trim(),
    premise: $("#in-premise").value.trim(),
    audience: $("#in-audience").value.trim(),
    extra: $("#in-extra").value.trim(),
    chapters: parseInt($("#in-chapters").value, 10) || 3,
    start_chapter: parseInt($("#in-start-chapter").value, 10) || 1,
    words_per_chapter: parseInt($("#in-words").value, 10) || 2000,
    total_words: parseInt($("#in-total-words").value, 10) || 100000,
    volume_words: parseInt($("#in-volume-words").value, 10) || 100000,
    batch_size: parseInt($("#in-batch-size").value, 10) || 3,
    skip_volume_review: $("#in-skip-volume-review").checked,
    skip_outline_review: $("#in-skip-outline-review").checked,
    skip_body_review: $("#in-skip-body-review").checked,
  };
}

async function startWorkflow() {
  const input = collectInput();
  if (!input.title) { showToast("请先填写书名"); return; }

  const btn = $("#btn-start");
  btn.disabled = true;
  btn.textContent = "⏳ 正在启动…";
  try {
    await apiPost("/api/settings", collectSettingsPayload());
    const res = await apiPost("/api/run", { input });
    state.workflowId = res.workflow_id;
    resetLive();
    switchView("run");
    connectStream(res.workflow_id);
  } catch (e) {
    showToast("启动失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "开始当前卷创作";
  }
}

/* ===================== SSE 实时流 ===================== */
function connectStream(id) {
  if (state.es) { state.es.close(); state.es = null; }
  const es = new EventSource("/api/workflows/" + id + "/stream");
  state.es = es;
  setLiveConnection("连接中", "connecting");
  es.onmessage = (e) => {
    setLiveConnection("实时连接", "connected");
    try { handleEvent(JSON.parse(e.data)); } catch (_) {}
  };
  es.onerror = () => { setLiveConnection("重连中", "reconnecting"); };
}

function setLiveConnection(label, status) {
  const el = $("#live-connection");
  if (!el) return;
  el.textContent = label;
  el.className = "live-connection " + status;
}

function handleEvent(msg) {
  const evt = msg.event;
  const data = msg.data || {};
  switch (evt) {
    case "snapshot":
    case "start":
    case "done":
      applyWorkflow(data);
      break;
    case "step_update":
      applyStep(data);
      break;
    case "review_required":
    case "background_review_required":
    case "chapter_review_required":
    case "body_review_required":
    case "handoff_review_required":
    case "review_approved":
      applyWorkflow(data);
      break;
    case "chunk":
      onChunk(data);
      break;
    case "progress":
      onProgress(data);
      break;
    case "chapter_done":
      onChapterDone(data);
      break;
    case "error":
      applyWorkflow(data);
      showToast("流程出错：" + (data.message || data.error_message || "未知错误"));
      break;
    case "end":
      applyWorkflow(data);
      if (state.es) { state.es.close(); state.es = null; }
      break;
  }
}

function applyWorkflow(wf) {
  if (!wf || !wf.steps) return;
  state.workflow = wf;
  for (const k of Object.keys(wf.steps)) {
    const st = wf.steps[k];
    if (st && st.status === "done" && st.output) state.stepOutputs[k] = st.output;
  }
  renderPipeline();
  renderRunHeader();
  renderActiveTab();
  updateDownload();
  renderReviewGate();
  renderBatchReviewGate();
}

function applyStep(step) {
  if (!state.workflow || !step || !step.key) return;
  state.workflow.steps[step.key] = step;
  if (step.status === "done" && step.output) state.stepOutputs[step.key] = step.output;
  renderPipeline();
  renderRunHeader();
  renderActiveTab();
  renderReviewGate();
  renderBatchReviewGate();
}

function onChunk(data) {
  const step = data.step;
  const delta = data.delta || "";
  if (!step || !delta) return;
  state.stepOutputs[step] = (state.stepOutputs[step] || "") + delta;
  state.liveChars += delta.length;
  if (step === "writer" || step === "polisher") state.chapterBuffer += delta;
  updateLiveOutput(step);
  $("#live-count").textContent = state.liveChars.toLocaleString("zh-CN") + " 字符";
  scheduleTabsRender();
}

function onProgress(data) {
  state.currentChapter = data.chapter || 0;
  state.chapterBuffer = "";
  const wrap = $("#progress-wrap");
  wrap.hidden = false;
  $("#progress-label").textContent = data.message || ("第 " + data.chapter + "/" + data.total + " 章");
  const total = data.total || 1;
  $("#progress-fill").style.width = ((state.currentChapter - 1) / total * 100) + "%";
  updateLiveOutput("writer");
}

function onChapterDone(data) {
  const no = data.chapter;
  const stage = data.stage;
  if (!no) return;
  if (!state.chapters[no - 1]) state.chapters[no - 1] = { no, draft: "", polished: "" };
  state.chapters[no - 1].no = no;
  if (stage === "draft") state.chapters[no - 1].draft = state.chapterBuffer;
  else if (stage === "polished") state.chapters[no - 1].polished = state.chapterBuffer;
  state.chapterBuffer = "";
  renderActiveTab();
}

function resetLive() {
  state.workflow = null;
  state.chapters = [];
  state.stepOutputs = {};
  state.chapterBuffer = "";
  state.currentChapter = 0;
  state.liveChars = 0;
  for (const k of STEP_KEYS) state.stepOutputs[k] = "";
  $("#live-output").textContent = "";
  $("#live-step-label").textContent = "";
  $("#progress-wrap").hidden = true;
  $("#progress-fill").style.width = "0%";
  $("#btn-download").disabled = true;
  $("#review-gate").hidden = true;
  $("#live-count").textContent = "0 字符";
  setLiveConnection("未连接", "idle");
}

/* ===================== 渲染 ===================== */
function switchView(view) {
  $$(".view").forEach((v) => v.classList.remove("active"));
  $("#view-" + view).classList.add("active");
}

function renderPipeline() {
  const el = $("#pipeline");
  if (!state.workflow) return;
  let html = "";
  PIPELINE_KEYS.forEach((k, i) => {
    const st = state.workflow.steps[k] || { status: "pending" };
    const icon = st.status === "done" ? "✓" : st.status === "running" ? "◌" : st.status === "error" ? "!" : "·";
    html += `<div class="pipe-step ${st.status}">
      <div class="pipe-dot">${icon}</div>
      <div class="pipe-label">${STEP_LABEL[k]}</div>
    </div>`;
    if (i < PIPELINE_KEYS.length - 1) html += `<div class="pipe-line ${st.status === "done" ? "done" : ""}"></div>`;
  });
  el.innerHTML = html;
}

function renderReviewGate() {
  const gate = $("#review-gate");
  const wf = state.workflow;
  const isAwaiting = wf && (wf.status === "awaiting_background_review" || wf.status === "awaiting_review" || wf.status === "awaiting_chapter_review" || wf.status === "awaiting_handoff_review");
  gate.hidden = !isAwaiting;
  if (!isAwaiting) return;
  const isBackground = wf.status === "awaiting_background_review";
  const isVolume = wf.status === "awaiting_review";
  const isHandoff = wf.status === "awaiting_handoff_review";
  $("#review-title").textContent = isBackground ? "确认背景与人物设定" : (isHandoff ? "审核卷末交接信息" : (isVolume ? "审核本卷规划" : `审核第 ${wf.current_chapter}-${wf.batch_end} 章细纲`));
  $("#review-desc").textContent = isBackground ? "背景设定是后续所有章节的依据。确认后才会开始本卷规划。" : (isHandoff ? "确认本卷收束和交接信息后，才会生成下一卷规划。" : (isVolume ? "确认本卷方向后，系统才会生成本轮连续章节细纲。" : "确认细纲后，系统会依次生成、润色并检查本轮正文。"));
  $("#review-label").textContent = isBackground ? "背景与人物设定" : (isHandoff ? "卷末交接信息" : (isVolume ? "本卷规划" : "本轮章节细纲"));
  $("#btn-approve-review").textContent = isBackground ? "确认背景，开始本卷规划" : (isHandoff ? "确认并开始下一卷" : (isVolume ? "确认卷规划" : "确认并开始写正文"));
  $("#btn-skip-review").hidden = isBackground;
  const outline = isBackground ? (wf.steps?.background?.output || state.stepOutputs.background || "") : (isHandoff ? (wf.steps?.handoff_review?.output || "") : (isVolume ? (wf.steps?.outline?.output || state.stepOutputs.outline || "") : (wf.steps?.chapter_outline?.output || state.stepOutputs.chapter_outline || "")));
  const field = $("#review-outline");
  if (field.value !== outline && !field.dataset.edited) field.value = outline;
}

function renderBatchReviewGate() {
  const gate = $("#batch-review-gate");
  const wf = state.workflow;
  const awaiting = wf && wf.status === "awaiting_body_review";
  gate.hidden = !awaiting;
  if (awaiting) $("#batch-review-summary").textContent = `第 ${Math.max(1, wf.current_chapter - (wf.input?.batch_size || 3))}-${wf.batch_end} 章已经完成并保存。确认后会继续下一轮连续章节。`;
}

async function continueBatch() {
  if (!state.workflowId) return;
  const btn = $("#btn-continue-batch");
  btn.disabled = true;
  try {
    const wf = await apiPost(`/api/workflows/${state.workflowId}/approve`, { outline: "" });
    applyWorkflow(wf);
    showToast("已确认，继续下一批");
  } catch (e) { showToast("继续失败：" + e.message); }
  finally { btn.disabled = false; }
}

async function skipReview() {
  if (!state.workflowId || !state.workflow) return;
  const btn = $("#btn-skip-review");
  btn.disabled = true;
  try {
    const content = $("#review-outline").value.trim();
    const wf = await apiPost(`/api/workflows/${state.workflowId}/approve`, { outline: content });
    applyWorkflow(wf);
    showToast("已跳过本次审核");
  } catch (e) { showToast("跳过失败：" + e.message); }
  finally { btn.disabled = false; }
}

async function approveReview() {
  if (!state.workflowId) return;
  const btn = $("#btn-approve-review");
  const outline = $("#review-outline").value.trim();
  if (!outline) { showToast("请保留或填写章节细纲"); return; }
  btn.disabled = true;
  btn.textContent = "正在确认...";
  try {
    const wf = await apiPost("/api/workflows/" + state.workflowId + "/approve", { outline });
    if (state.workflow?.status === "awaiting_background_review") state.stepOutputs.background = outline;
    else if (state.workflow?.status === "awaiting_review") state.stepOutputs.outline = outline;
    else state.stepOutputs.chapter_outline = outline;
    $("#review-outline").dataset.edited = "";
    applyWorkflow(wf);
    showToast("审核已确认，流程继续执行");
  } catch (e) {
    showToast("确认失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "确认并继续";
  }
}

function renderRunHeader() {
  const wf = state.workflow;
  if (!wf) return;
  $("#run-title").textContent = wf.input?.title || "未命名小说";
  const meta = [wf.input?.genre, wf.input?.total_words ? "目标 " + Number(wf.input.total_words).toLocaleString() + " 字" : "", wf.current_volume ? "当前第 " + wf.current_volume + " 卷" : "", wf.input?.chapters ? "预计 " + wf.input.chapters + " 章" : "", wf.input?.batch_size ? "每轮 " + wf.input.batch_size + " 章" : ""].filter(Boolean).join(" · ");
  $("#run-meta").textContent = meta;
  const badge = $("#run-status-badge");
  badge.textContent = statusText(wf.status);
  badge.className = "badge " + (wf.status || "pending");
  $("#btn-retry").hidden = wf.status !== "error";
  const chapterInput = $("#in-select-chapter");
  chapterInput.max = wf.input?.chapters || 1;
  if (!chapterInput.dataset.edited) chapterInput.value = Math.min(wf.current_chapter || 1, wf.input?.chapters || 1);
}

async function selectChapter() {
  if (!state.workflowId || !state.workflow) return;
  const input = $("#in-select-chapter");
  const chapter = parseInt(input.value, 10);
  if (!chapter) { showToast("请输入要开始生成的章节号"); return; }
  const btn = $("#btn-select-chapter");
  btn.disabled = true;
  try {
    const wf = await apiPost(`/api/workflows/${state.workflowId}/select-chapter`, { chapter });
    input.dataset.edited = "";
    applyWorkflow(wf);
    connectStream(state.workflowId);
    showToast(`将从第 ${chapter} 章开始生成，已有章节不会删除`);
  } catch (e) { showToast("无法选择章节：" + e.message); }
  finally { btn.disabled = false; }
}

async function retryWorkflow() {
  if (!state.workflowId) return;
  const btn = $("#btn-retry");
  btn.disabled = true;
  btn.textContent = "正在重试...";
  try {
    const wf = await apiPost("/api/workflows/" + state.workflowId + "/retry", {});
    applyWorkflow(wf);
    connectStream(state.workflowId);
  } catch (e) {
    showToast("重试失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "↻ 重试";
  }
}

function updateLiveOutput(step) {
  let label = STEP_LABEL[step] || step || "";
  if ((step === "writer" || step === "polisher") && state.currentChapter) label += " · 第 " + state.currentChapter + " 章";
  $("#live-step-label").textContent = label;
  const text = (step === "writer" || step === "polisher")
    ? state.chapterBuffer
    : (state.stepOutputs[step] || "");
  const el = $("#live-output");
  el.textContent = text;
  el.scrollTop = el.scrollHeight;
}

let tabsTimer = null;
function scheduleTabsRender() {
  clearTimeout(tabsTimer);
  tabsTimer = setTimeout(renderActiveTab, 400);
}

function renderActiveTab() {
  const wf = state.workflow;
  const tab = state.activeTab;
  const panels = $("#tab-panels");
  let html = "";

  if (tab === "background") {
    html = mdWrap(state.stepOutputs.background || wf?.steps?.background?.output || "");
  } else if (tab === "outline") {
    const volume = state.stepOutputs.outline || wf?.steps?.outline?.output || "";
    const batch = state.stepOutputs.chapter_outline || wf?.steps?.chapter_outline?.output || "";
    html = mdWrap((volume ? "# 本卷规划\n\n" + volume : "") + (batch ? "\n\n# 本轮章节细纲\n\n" + batch : ""));
  } else if (tab === "archive") {
    html = mdWrap(state.stepOutputs.archiver || wf?.steps?.archiver?.output || "");
  } else if (tab === "chapters") {
    html = renderChaptersPanel();
  }

  panels.innerHTML = html;
  $$("#result-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
}

function mdWrap(text) {
  return `<div class="md">${mdRender(text) || '<div class="empty">暂无内容</div>'}</div>`;
}

function renderChaptersPanel() {
  if (!state.chapters.length) {
    return '<div class="empty">正文尚未生成。若为历史记录，请确认该工作流已产出章节。</div>';
  }
  return state.chapters.map((c) => {
    const body = c.polished || c.draft || "";
    return `<div class="chapter-block">
      <h4>第 ${c.no} 章</h4>
      <div class="chapter-seg">
        <button class="seg ${c.polished ? "active" : ""}" data-seg="polished" data-ch="${c.no}">润色稿</button>
        <button class="seg ${!c.polished ? "active" : ""}" data-seg="draft" data-ch="${c.no}">初稿</button>
      </div>
      <div class="chapter-body">${escHtml(body)}</div>
    </div>`;
  }).join("");
}

function bindChapterSegs() {
  $$(".chapter-seg").forEach((segWrap) => {
    segWrap.querySelectorAll(".seg").forEach((btn) => {
      btn.addEventListener("click", () => {
        segWrap.querySelectorAll(".seg").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const no = parseInt(btn.dataset.ch, 10);
        const seg = btn.dataset.seg;
        const c = state.chapters.find((x) => x.no === no);
        if (c) segWrap.parentElement.querySelector(".chapter-body").textContent = seg === "draft" ? (c.draft || "") : (c.polished || c.draft || "");
      });
    });
  });
}

function updateDownload() {
  const wf = state.workflow;
  $("#btn-download").disabled = !(wf && wf.archive_path);
}

/* ===================== Markdown 简易渲染 ===================== */
function inlineMd(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

function mdRender(src) {
  if (!src) return "";
  const lines = escHtml(src).split("\n");
  let html = "";
  let list = null;
  const closeList = () => { if (list) { html += "</" + list + ">"; list = null; } };

  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^######\s+(.*)/))) { closeList(); html += "<h6>" + inlineMd(m[1]) + "</h6>"; }
    else if ((m = line.match(/^#####\s+(.*)/))) { closeList(); html += "<h5>" + inlineMd(m[1]) + "</h5>"; }
    else if ((m = line.match(/^####\s+(.*)/))) { closeList(); html += "<h4>" + inlineMd(m[1]) + "</h4>"; }
    else if ((m = line.match(/^###\s+(.*)/))) { closeList(); html += "<h3>" + inlineMd(m[1]) + "</h3>"; }
    else if ((m = line.match(/^##\s+(.*)/))) { closeList(); html += "<h2>" + inlineMd(m[1]) + "</h2>"; }
    else if ((m = line.match(/^#\s+(.*)/))) { closeList(); html += "<h1>" + inlineMd(m[1]) + "</h1>"; }
    else if ((m = line.match(/^[-*]\s+(.*)/))) { if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; } html += "<li>" + inlineMd(m[1]) + "</li>"; }
    else if ((m = line.match(/^\d+\.\s+(.*)/))) { if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; } html += "<li>" + inlineMd(m[1]) + "</li>"; }
    else if ((m = line.match(/^&gt;\s?(.*)/))) { closeList(); html += "<blockquote>" + inlineMd(m[1]) + "</blockquote>"; }
    else if (/^-{3,}$/.test(line.trim())) { closeList(); html += "<hr>"; }
    else if (line.trim() === "") { closeList(); }
    else { closeList(); html += "<p>" + inlineMd(line) + "</p>"; }
  }
  closeList();
  return html;
}

/* ===================== 历史记录 ===================== */
async function openHistory() {
  $("#history-modal").hidden = false;
  const list = $("#history-list");
  list.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const data = await apiGet("/api/workflows");
    const wfs = data.workflows || [];
    if (!wfs.length) { list.innerHTML = '<div class="empty">暂无历史记录。新建项目后，记录和断点会保存在桌面 NovelFlow作品 目录。</div>'; return; }
    list.innerHTML = wfs.map((w) => `
      <div class="history-item" data-id="${w.id}">
        <div class="hi-main">
          <strong>${escHtml(w.input?.title || "未命名")}</strong>
          <span class="badge ${w.status}">${statusText(w.status)}</span>
        </div>
        <div class="hi-sub">${escHtml(w.input?.genre || "")} · ${w.input?.chapters || 0} 章 · ${formatTime(w.created_at)}</div>
      </div>`).join("");
    list.querySelectorAll(".history-item").forEach((item) => {
      item.addEventListener("click", () => openWorkflow(item.dataset.id));
    });
  } catch (e) {
    list.innerHTML = `<div class="empty">加载失败：${escHtml(e.message)}</div>`;
  }
}

async function openWorkflow(id) {
  $("#history-modal").hidden = true;
  resetLive();
  state.workflowId = id;
  try {
    const wf = await apiGet("/api/workflows/" + id);
    applyWorkflow(wf);
    const ch = await apiGet("/api/workflows/" + id + "/chapters");
    state.chapters = ch.chapters || [];
    switchView("run");
    renderActiveTab();
    if (wf.status === "running" || wf.status === "pending" || wf.status.startsWith("awaiting_")) connectStream(id);
  } catch (e) {
    showToast("打开失败：" + e.message);
  }
}

function downloadArchive() {
  if (!state.workflowId) return;
  if (state.workflow && state.workflow.archive_path) {
    window.open("/api/workflows/" + state.workflowId + "/download", "_blank");
  } else {
    showToast("归档文件尚未生成");
  }
}

/* ===================== Toast ===================== */
let toastTimer = null;
function showToast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  requestAnimationFrame(() => t.classList.add("show"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    t.classList.remove("show");
    setTimeout(() => { t.hidden = true; }, 250);
  }, 3400);
}

/* ===================== 事件绑定 & 初始化 ===================== */
function bindStaticEvents() {
  $("#btn-start").addEventListener("click", startWorkflow);
  $("#btn-settings").addEventListener("click", openSettings);
  $("#btn-api-view").addEventListener("click", openApiViewer);
  $("#btn-close-api").addEventListener("click", closeApiViewer);
  $("#btn-clear-settings-history").addEventListener("click", clearSettingsHistory);
  $("#btn-close-settings").addEventListener("click", closeSettings);
  $("#btn-save-settings").addEventListener("click", saveSettings);
  $("#btn-history").addEventListener("click", openHistory);
  $("#btn-new").addEventListener("click", () => { if (state.es) { state.es.close(); state.es = null; } switchView("create"); });
  $("#btn-close-history").addEventListener("click", () => { $("#history-modal").hidden = true; });
  $("#btn-download").addEventListener("click", downloadArchive);
  $("#btn-retry").addEventListener("click", retryWorkflow);
  $("#btn-select-chapter").addEventListener("click", selectChapter);
  $("#btn-approve-review").addEventListener("click", approveReview);
  $("#btn-skip-review").addEventListener("click", skipReview);
  $("#btn-continue-batch").addEventListener("click", continueBatch);
  $("#review-outline").addEventListener("input", () => { $("#review-outline").dataset.edited = "true"; });
  $("#in-select-chapter").addEventListener("input", () => { $("#in-select-chapter").dataset.edited = "true"; });
  $("#history-modal").addEventListener("click", (e) => { if (e.target.id === "history-modal") e.target.hidden = true; });
  $("#settings-modal").addEventListener("click", (e) => { if (e.target.id === "settings-modal") closeSettings(); });
  $("#api-modal").addEventListener("click", (e) => { if (e.target.id === "api-modal") closeApiViewer(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#settings-modal").hidden) closeSettings();
    if (e.key === "Escape" && !$("#api-modal").hidden) closeApiViewer();
  });

  $$("#result-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => { state.activeTab = tab.dataset.tab; renderActiveTab(); });
  });

  // 章节切换事件委托
  $("#tab-panels").addEventListener("click", (e) => {
    const seg = e.target.closest(".seg");
    if (!seg) return;
    const block = seg.closest(".chapter-block");
    block.querySelectorAll(".seg").forEach((b) => b.classList.remove("active"));
    seg.classList.add("active");
    const no = parseInt(seg.dataset.ch, 10);
    const c = state.chapters.find((x) => x.no === no);
    if (c) block.querySelector(".chapter-body").textContent = seg.dataset.seg === "draft" ? (c.draft || "") : (c.polished || c.draft || "");
  });
}

async function init() {
  bindStaticEvents();
  try {
    const recalc = () => {
      const total = parseInt($("#in-total-words").value, 10) || 0;
      const words = parseInt($("#in-words").value, 10) || 2000;
      const volumeWords = parseInt($("#in-volume-words").value, 10) || 100000;
      const chapters = Math.max(1, Math.ceil(total / words));
      const chaptersPerVolume = Math.max(1, Math.ceil(volumeWords / words));
      $("#in-chapters").value = chapters;
      $("#in-volumes").value = Math.max(1, Math.ceil(chapters / chaptersPerVolume));
      $("#in-start-chapter").max = chapters;
      if (parseInt($("#in-start-chapter").value, 10) > chapters) $("#in-start-chapter").value = chapters;
    };
    $("#in-total-words").addEventListener("input", recalc);
    $("#in-words").addEventListener("input", recalc);
    $("#in-volume-words").addEventListener("input", recalc);
    recalc();
    await loadSettings();
  } catch (e) {
    showToast("加载设置失败：" + e.message);
  }
}

init();
