"use strict";

/* HIP 仿真服务运维面板(原生 JS,零依赖)。
 * 安全约定:服务端来的一切文本(错误消息/日志/文件名/ID/stages label)
 * 一律经 textContent 渲染,严禁拼进 innerHTML。 */

// ===== 常量 =====
const STATUS_META = {
  pending: { label: "待执行", cls: "st-pending" },
  running: { label: "运行中", cls: "st-running" },
  succeeded: { label: "已成功", cls: "st-succeeded" },
  failed: { label: "已失败", cls: "st-failed" },
  cancelled: { label: "已取消", cls: "st-cancelled" },
};
const TERMINAL_STATUSES = ["succeeded", "failed", "cancelled"];
const DEFAULT_INTERVAL_MS = 3000;
const LOG_FOLLOW_MS = 2000;
const MAX_RENDER_LINES = 2000; // 大日志渲染上限:只渲染末 N 行,防 DOM 爆炸
const PIN_TO_BOTTOM_PX = 24; // 距底小于该值(像素)视为"钉在底部"

// ===== 状态(不可变更新:一律整体替换,不改原对象)=====
// 定时器句柄与请求序号是模块级可变引用(非视图状态),不进 state
let state = {
  health: null,
  jobs: [],
  filter: "all",
  autoRefresh: true,
  intervalMs: DEFAULT_INTERVAL_MS,
  activeTab: "jobs",
  serviceAuto: true,
  drawer: {
    jobId: null,
    job: null, // 末次已知 JobState 快照(作业从列表消失后仍可看)
    listed: true, // 作业是否仍在 /jobs 列表(消失 = 视为终态,停止续追)
    source: "job.log",
    tail: 200, // 200 | 1000 | 0(0 = 全文,不加 tail 参数)
    follow: true,
  },
};
let pollTimer = null;
let logTimer = null;

// ===== DOM 小工具 =====
function $(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

// ===== fetch 层 =====
async function readErrorBody(resp) {
  // 错误体统一 {code, message};解析失败退回 HTTP 状态描述
  let code = "HTTP_" + resp.status;
  let message = "请求失败(HTTP " + resp.status + ")";
  try {
    const body = await resp.json();
    if (body && typeof body.code === "string") {
      code = body.code;
      if (typeof body.message === "string") message = body.message;
    }
  } catch (_err) {
    // 非 JSON 错误体,保留 HTTP 口径
  }
  const error = new Error(message);
  error.code = code;
  return error;
}

async function fetchJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw await readErrorBody(resp);
  return resp.json();
}

async function fetchText(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw await readErrorBody(resp);
  return resp.text();
}

// 日志专用拉取:服务端对超 2MB 文件缺省截尾 2000 行并带 X-Log-Truncated 头
async function fetchLog(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw await readErrorBody(resp);
  return {
    text: await resp.text(),
    truncated: resp.headers.get("X-Log-Truncated") === "true",
  };
}

function showTruncationHint(box) {
  // 置顶提示行不计入渲染截断的行数切片(log-skip 弱化样式)
  box.insertBefore(
    el("div", "log-line log-skip", "(文件过大,服务端已截尾至末 2000 行)"),
    box.firstChild
  );
}

// ===== 错误条 =====
function showError(message) {
  const bar = $("error-bar");
  bar.textContent = "请求异常:" + message;
  bar.hidden = false;
}

function clearError() {
  $("error-bar").hidden = true;
}

// ===== 格式化 =====
function formatTime(iso) {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function humanizeDuration(totalSeconds) {
  if (totalSeconds < 60) return totalSeconds.toFixed(1) + " 秒";
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return minutes + " 分 " + Math.round(totalSeconds % 60) + " 秒";
  return Math.floor(minutes / 60) + " 时 " + (minutes % 60) + " 分";
}

function formatElapsed(job) {
  if (job.status === "pending") return "-";
  const start = Date.parse(job.created_at);
  const end = TERMINAL_STATUSES.includes(job.status)
    ? Date.parse(job.finished_at)
    : Date.now(); // 运行中:至今
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return "-";
  return humanizeDuration((end - start) / 1000);
}

function stageSummary(stages) {
  if (!stages || stages.length === 0) return "-";
  const last = stages[stages.length - 1];
  return (last && last.label ? last.label : "-") + "(共 " + stages.length + " 段)";
}

// ===== 日志逐行渲染(时间戳本地化 + 着色 + 行数上限)=====
function pad2(value) {
  return String(value).padStart(2, "0");
}

function localLogTimestamp(raw) {
  // 服务端时间戳为 UTC ISO 8601(+00:00 后缀),转浏览器本地时间(定长 YYYY-MM-DD HH:mm:ss);
  // 其余格式(如 job.out 内文的 [NOTE])不匹配即原样保留
  if (!/^\d{4}-\d{2}-\d{2}[T ]/.test(raw)) return null;
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return null;
  const base =
    date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) +
    " " + pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
  const ms = date.getMilliseconds();
  return ms > 0 ? base + "." + String(ms).padStart(3, "0") : base;
}

function classifyServiceLine(text) {
  const match = /" (\d{3}) /.exec(text) || /" (\d{3})$/.exec(text);
  if (!match) return "";
  const code = Number(match[1]);
  if (code >= 500) return "log-error";
  if (code >= 400) return "log-warn";
  return "log-ok";
}

function classifyJobLine(text) {
  // 覆盖 job.log 状态事件(状态变更 X → Y [CODE] msg)与 job.out 的 MAPDL 输出;
  // 检查顺序 = 终态优先(否则"running → cancelled"会被过渡词 running 抢先命中)
  if (/ERROR|错误|失败|FAILED|FATAL|PROBLEM TERMINATED/i.test(text)) return "log-error";
  if (/WARNING|警告/i.test(text)) return "log-warn";
  if (/succeeded|成功|完成/i.test(text)) return "log-ok";
  if (/cancelled|已取消/i.test(text)) return "log-cancel";
  if (/running|运行中/i.test(text)) return "log-info";
  return "";
}

function classifyLogLine(text, kind) {
  return kind === "service" ? classifyServiceLine(text) : classifyJobLine(text);
}

function appendLogLine(fragment, line, kind) {
  // 时间戳段独立成弱化灰 span,正文紧跟其后;全程 createElement/textContent
  const node = el("div", "log-line " + classifyLogLine(line, kind));
  const match = /^(\[[^\]]+\])([\s\S]*)$/.exec(line);
  const local = match ? localLogTimestamp(match[1].slice(1, -1)) : null;
  if (local) {
    node.appendChild(el("span", "log-ts", "[" + local + "]"));
    node.appendChild(document.createTextNode(match[2]));
  } else {
    node.appendChild(document.createTextNode(line === "" ? " " : line)); // 空行占位保高度
  }
  fragment.appendChild(node);
}

function renderLogLines(container, text, kind) {
  const lines = text.length === 0 ? [] : text.split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop(); // 去掉末尾换行产生的空行
  const omitted = Math.max(0, lines.length - MAX_RENDER_LINES);
  const fragment = document.createDocumentFragment();
  if (omitted > 0) {
    fragment.appendChild(
      el("div", "log-line log-skip", "(仅显示末 " + MAX_RENDER_LINES + " 行,共 " + lines.length + " 行)")
    );
  }
  lines.slice(omitted).forEach((line) => appendLogLine(fragment, line, kind));
  if (fragment.childNodes.length === 0) {
    fragment.appendChild(el("div", "log-line muted", "(空日志)"));
  }
  container.replaceChildren(fragment);
}

function showLogLoading(container, message) {
  container.replaceChildren(el("div", "log-line muted", message));
}

// ===== 健康条渲染 =====
function setHealthBadge(node, ok, okText, badText, okCls, badCls) {
  node.textContent = ok ? okText : badText;
  node.className = "badge " + (ok ? okCls : badCls);
}

function setReadyIndicator(node, ok, name, badText) {
  node.textContent = (ok ? "● " : "○ ") + name + (ok ? "就绪" : badText);
  node.className = "hb-item " + (ok ? "ok" : "bad");
}

function renderHealth(health) {
  setHealthBadge($("hb-status"), health.status === "ok", "正常", "降级", "st-succeeded", "st-degraded");
  $("hb-version").textContent = health.version ?? "-";
  $("hb-running").textContent = health.queue_running ?? "-";
  $("hb-pending").textContent = health.queue_pending ?? "-";
  setHealthBadge($("hb-passthrough"), health.passthrough_enabled === true, "直通:开", "直通:关", "st-running", "st-pending");
  setReadyIndicator($("hb-mapdl"), health.mapdl_found === true, "MAPDL ", "缺失");
  setReadyIndicator($("hb-license"), health.license_env_set === true, "许可 ", "未设置");
}

// ===== 作业表渲染 =====
function statusMeta(status) {
  return STATUS_META[status] || { label: status, cls: "" };
}

function statusCell(status) {
  const meta = statusMeta(status);
  const cell = el("td");
  cell.appendChild(el("span", "badge " + meta.cls, meta.label));
  return cell;
}

function jobRow(job) {
  const row = el("tr");
  if (state.drawer.jobId === job.id) row.classList.add("selected");
  row.appendChild(el("td", "mono", job.id));
  row.appendChild(statusCell(job.status));
  row.appendChild(el("td", null, job.method));
  row.appendChild(el("td", null, formatTime(job.created_at)));
  row.appendChild(el("td", null, formatElapsed(job)));
  row.appendChild(el("td", null, stageSummary(job.stages)));
  row.appendChild(el("td", "mono", job.error ? job.error.code : ""));
  const actions = el("td");
  if (!TERMINAL_STATUSES.includes(job.status)) {
    actions.appendChild(stopButton(job));
  }
  const button = el("button", "btn", "详情");
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    openDrawer(job.id);
  });
  actions.appendChild(button);
  row.appendChild(actions);
  row.addEventListener("click", () => openDrawer(job.id));
  return row;
}

function visibleJobs() {
  if (state.filter === "all") return state.jobs;
  return state.jobs.filter((job) => job.status === state.filter);
}

// 强制中断按钮:POST /jobs/{id}/cancel(killpg 同步执行,最坏 ~5s,期间按钮
// 置灰防重复提交);中断保留现场,失败经错误条提示
function stopButton(job) {
  const button = el("button", "btn btn-stop", "强制中断");
  button.addEventListener("click", async (event) => {
    event.stopPropagation();
    button.disabled = true;
    button.textContent = "中断中…";
    try {
      const resp = await fetch("/jobs/" + job.id + "/cancel", { method: "POST" });
      if (!resp.ok) throw await readErrorBody(resp);
    } catch (err) {
      showError("强制中断失败:" + err.message);
      button.disabled = false;
      button.textContent = "强制中断";
      return;
    }
    try {
      await refreshJobs();
    } catch (err) {
      showError("刷新作业列表失败:" + err.message);
    }
  });
  return button;
}

function renderJobs() {
  const tbody = $("job-rows");
  const jobs = visibleJobs();
  if (jobs.length === 0) {
    const cell = el("td", "muted", "暂无作业");
    cell.colSpan = 8;
    const row = el("tr");
    row.appendChild(cell);
    tbody.replaceChildren(row);
    return;
  }
  tbody.replaceChildren(...jobs.map(jobRow));
}

// ===== 详情抽屉:静态骨架填充 =====
function renderStatusInto(node, status) {
  const meta = statusMeta(status);
  node.textContent = meta.label;
  node.className = "badge " + meta.cls;
}

function renderStages(stages) {
  const tbody = $("stages-body");
  const empty = $("stages-empty");
  if (!stages || stages.length === 0) {
    tbody.replaceChildren();
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  tbody.replaceChildren(
    ...stages.map((stage) => {
      const row = el("tr");
      row.appendChild(el("td", "mono", stage.label));
      row.appendChild(el("td", null, stage.time_s));
      return row;
    })
  );
}

function renderError(job) {
  const box = $("error-box");
  if (!job.error) {
    box.hidden = true;
    return;
  }
  $("error-code").textContent = job.error.code ?? "-";
  $("error-message").textContent = job.error.message ?? "";
  box.hidden = false;
}

function renderDrawer() {
  const job = state.drawer.job;
  if (!job) return;
  $("drawer-title").textContent = job.id;
  renderStatusInto($("drawer-status"), job.status);
  $("drawer-vanished").hidden = state.drawer.listed; // 消失提示仅未列出时显示
  $("drawer-method").textContent = job.method ?? "-";
  $("drawer-fidelity").textContent = job.fidelity ?? "-";
  $("drawer-part").textContent = job.part ?? "-";
  $("tl-created").textContent = formatTime(job.created_at);
  $("tl-started").textContent = formatTime(job.started_at);
  $("tl-finished").textContent = formatTime(job.finished_at);
  renderStages(job.stages);
  renderError(job);
}

// ===== 详情抽屉:结果与工件(按需异步)=====
function resetResultArea(pending) {
  $("result-values").replaceChildren();
  $("result-error").hidden = true;
  $("result-empty").hidden = !pending;
  $("result-empty").textContent = pending ? "结果加载中…" : "作业未成功,无结果数值";
}

function renderResult(result) {
  $("result-empty").hidden = true;
  $("result-error").hidden = true;
  const values = result && typeof result.values === "object" ? result.values : {};
  const entries = Object.entries(values);
  if (entries.length === 0) {
    $("result-empty").textContent = "结果无 values 字段(纯搬运作业)";
    $("result-empty").hidden = false;
    return;
  }
  $("result-values").replaceChildren(
    ...entries.map(([label, value]) => {
      const row = el("tr");
      row.appendChild(el("td", "mono", label));
      row.appendChild(el("td", null, value));
      return row;
    })
  );
}

async function loadResult(job) {
  if (job.status !== "succeeded" || !job.result_url) {
    resetResultArea(false);
    return;
  }
  resetResultArea(true);
  try {
    renderResult(await fetchJson(job.result_url));
  } catch (err) {
    // 典型 409:未成功作业取结果;显示服务端 code/message
    $("result-empty").hidden = true;
    $("result-error").textContent = err.code + ":" + err.message;
    $("result-error").hidden = false;
  }
}

function artifactsUrl(job) {
  return job.artifacts_url || "/jobs/" + encodeURIComponent(job.id) + "/artifacts";
}

function appendArtifactLink(list, job, name) {
  const link = el("a", null, name);
  link.href = artifactsUrl(job) + "/" + encodeURIComponent(name);
  const item = el("li");
  item.appendChild(link);
  list.appendChild(item);
}

async function loadArtifacts(job) {
  const list = $("artifacts-list");
  list.replaceChildren(el("li", "muted", "加载中…"));
  try {
    const names = await fetchJson(artifactsUrl(job));
    list.replaceChildren();
    if (!Array.isArray(names) || names.length === 0) {
      list.appendChild(el("li", "muted", "无工件"));
      return;
    }
    names.forEach((name) => appendArtifactLink(list, job, name));
  } catch (err) {
    list.replaceChildren(el("li", "muted", "工件列表加载失败:" + err.message));
  }
}

// ===== 详情抽屉:日志查看器 =====
function buildLogUrl(job, source, tail) {
  const params = new URLSearchParams();
  params.set("source", source);
  if (tail) params.set("tail", String(tail)); // 0 = 全文,不加参数
  return (job.log_url || "/jobs/" + encodeURIComponent(job.id) + "/log") + "?" + params.toString();
}

// 抽屉日志请求序号:旧响应(续追定时器/切源/切档并发)不得覆盖新请求的结果
let drawerLogSeq = 0;

function isPinnedToBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= PIN_TO_BOTTOM_PX;
}

// 量测→渲染→条件滚动:替换 DOM 前先记下旧视图是否钉底,续追刷新不打扰
// 上翻阅读历史的用户。stick 意图显式声明,不靠盒子几何副作用推断:
//   true = 新视图无条件落底(首开/切源/切档)
//   "auto" = 原本钉底才落底(轮询续追)
//   false = 不滚动(错误覆写)
function renderLogPinned(box, text, kind, stick) {
  const wasPinned = isPinnedToBottom(box);
  renderLogLines(box, text, kind);
  if (stick === true || (stick === "auto" && wasPinned)) box.scrollTop = box.scrollHeight;
}

async function fetchDrawerLog(stick = "auto") {
  const drawer = state.drawer;
  if (!drawer.job || !drawer.jobId) return;
  const seq = ++drawerLogSeq;
  const jobId = drawer.jobId; // 身份锚点:响应回来时抽屉可能已切到别的作业
  try {
    const { text, truncated } = await fetchLog(buildLogUrl(drawer.job, drawer.source, drawer.tail));
    if (seq !== drawerLogSeq || state.drawer.jobId !== jobId) return;
    renderLogPinned($("log-content"), text, "job", drawer.follow ? stick : false);
    if (truncated) showTruncationHint($("log-content"));
  } catch (err) {
    if (seq !== drawerLogSeq || state.drawer.jobId !== jobId) return;
    renderLogPinned($("log-content"), "日志加载失败:" + err.code + ":" + err.message, "job", false);
  }
}

function syncLogTimer() {
  const drawer = state.drawer;
  // listed 参与判定:作业从列表消失(被 DELETE/清扫)即视为终态,续追停摆
  // (否则末次 running 快照会每 2s 打一次 404 覆写日志窗格)
  const shouldFollow = Boolean(
    drawer.jobId && drawer.follow && drawer.listed &&
    drawer.job && drawer.job.status === "running"
  );
  if (shouldFollow && logTimer === null) {
    logTimer = setInterval(fetchDrawerLog, LOG_FOLLOW_MS);
  } else if (!shouldFollow && logTimer !== null) {
    clearInterval(logTimer);
    logTimer = null;
  }
}

// ===== 详情抽屉:开关 =====
function openDrawer(jobId) {
  const known = state.jobs.find((job) => job.id === jobId);
  state = {
    ...state,
    drawer: {
      ...state.drawer,
      jobId,
      job: known || state.drawer.job,
      listed: Boolean(known), // 从表格行打开必然在列;防御性兜底
    },
  };
  $("drawer").classList.add("open");
  // 日志为异步取数,先铺加载态(切作业时不残留上一个作业的日志);
  // result/artifacts 的加载态由各自 loader 同步首行负责
  showLogLoading($("log-content"), "日志加载中…");
  renderDrawer();
  renderJobs(); // 选中行高亮即时切换
  if (state.drawer.job) {
    loadResult(state.drawer.job);
    loadArtifacts(state.drawer.job);
  }
  fetchDrawerLog(true); // 新视图显式落底(不依赖加载占位符的几何副作用)
  syncLogTimer();
}

function closeDrawer() {
  state = { ...state, drawer: { ...state.drawer, jobId: null, job: null } };
  $("drawer").classList.remove("open");
  renderJobs(); // 清除选中行高亮
  syncLogTimer();
}

// ===== 服务日志 tab =====
let serviceLogSeq = 0; // 请求序号:切档位/自动刷新并发时旧响应不得覆盖新请求

async function refreshServiceLog() {
  const seq = ++serviceLogSeq;
  const tail = Number($("service-tail").value); // 0 = 全文
  const url = "/service/log" + (tail ? "?tail=" + tail : "");
  const box = $("service-log-content");
  try {
    const { text, truncated } = await fetchLog(url);
    if (seq !== serviceLogSeq) return;
    // 自动刷新只跟随"原本就在底部"的视图:上翻阅读历史不被拽回最新
    renderLogPinned(box, text, "service", "auto");
    if (truncated) showTruncationHint(box);
  } catch (err) {
    if (seq !== serviceLogSeq) return;
    renderLogPinned(box, "服务日志加载失败:" + err.code + ":" + err.message, "service", false);
  }
}

// ===== 轮询层 =====
async function refreshHealth() {
  const health = await fetchJson("/health");
  state = { ...state, health };
  renderHealth(health);
}

function syncDrawerFromJobs() {
  const drawer = state.drawer;
  if (!drawer.jobId) return;
  const job = state.jobs.find((item) => item.id === drawer.jobId);
  if (!job) {
    // 已删除/被清扫:保留末次快照供查看,但标记消失 → 停止续追 + 概览提示
    if (drawer.listed) {
      state = { ...state, drawer: { ...drawer, listed: false } };
      renderDrawer();
      syncLogTimer();
    }
    return;
  }
  const statusChanged = drawer.job && drawer.job.status !== job.status;
  state = { ...state, drawer: { ...drawer, job, listed: true } };
  renderDrawer();
  if (statusChanged) {
    loadResult(job);
    loadArtifacts(job);
    fetchDrawerLog();
  }
  syncLogTimer();
}

async function refreshJobs() {
  const jobs = await fetchJson("/jobs");
  if (!Array.isArray(jobs)) throw new Error("/jobs 返回格式异常(非数组)");
  state = { ...state, jobs };
  renderJobs();
  syncDrawerFromJobs();
}

let pollInFlight = false;

async function pollOnce() {
  // 在途守卫:请求慢于轮询间隔时跳过本轮,防 /jobs 全目录扫描堆叠、
  // 慢的旧响应后到覆盖新数据(与日志层的请求序号守卫同目的)
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    // 永不抛出:轮询定时器不能因单次失败死掉
    const tasks = [refreshHealth()];
    if (state.activeTab === "jobs") tasks.push(refreshJobs());
    if (state.activeTab === "service" && state.serviceAuto) tasks.push(refreshServiceLog());
    const results = await Promise.allSettled(tasks);
    const failure = results.find((item) => item.status === "rejected");
    if (failure) showError(failure.reason.message);
    else clearError();
  } finally {
    pollInFlight = false;
  }
}

function restartPollTimer() {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (!state.autoRefresh) return;
  pollTimer = setInterval(() => {
    pollOnce();
  }, state.intervalMs);
}

// ===== tab 切换 =====
function switchTab(tab) {
  state = { ...state, activeTab: tab };
  $("tab-jobs").classList.toggle("active", tab === "jobs");
  $("tab-service").classList.toggle("active", tab === "service");
  $("panel-jobs").hidden = tab !== "jobs";
  $("panel-service").hidden = tab !== "service";
  pollOnce(); // 切换立即取一次
}

// ===== 事件绑定与启动 =====
function bindToolbarEvents() {
  $("filter-select").addEventListener("change", (event) => {
    state = { ...state, filter: event.target.value };
    renderJobs();
  });
  $("auto-refresh").addEventListener("change", (event) => {
    state = { ...state, autoRefresh: event.target.checked };
    restartPollTimer();
    if (state.autoRefresh) pollOnce();
  });
  $("interval-select").addEventListener("change", (event) => {
    state = { ...state, intervalMs: Number(event.target.value) };
    restartPollTimer();
  });
  $("refresh-btn").addEventListener("click", () => pollOnce());
}

function bindTabEvents() {
  $("tab-jobs").addEventListener("click", () => switchTab("jobs"));
  $("tab-service").addEventListener("click", () => switchTab("service"));
  $("service-tail").addEventListener("change", () => {
    showLogLoading($("service-log-content"), "日志加载中…");
    refreshServiceLog();
  });
  $("service-auto").addEventListener("change", (event) => {
    state = { ...state, serviceAuto: event.target.checked };
  });
}

function bindDrawerEvents() {
  $("drawer-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
  });
  $("log-source").addEventListener("change", (event) => {
    state = { ...state, drawer: { ...state.drawer, source: event.target.value } };
    showLogLoading($("log-content"), "日志加载中…"); // 即时反馈:请求在途时先换掉旧源内容
    fetchDrawerLog(true); // 切源 = 新视图,显式落底
  });
  $("log-tail").addEventListener("change", (event) => {
    state = { ...state, drawer: { ...state.drawer, tail: Number(event.target.value) } };
    showLogLoading($("log-content"), "日志加载中…");
    fetchDrawerLog(true); // 切档 = 新视图,显式落底
  });
  $("log-follow").addEventListener("change", (event) => {
    state = { ...state, drawer: { ...state.drawer, follow: event.target.checked } };
    syncLogTimer();
  });
}

function init() {
  bindToolbarEvents();
  bindTabEvents();
  bindDrawerEvents();
  showLogLoading($("log-content"), "尚无日志");
  showLogLoading($("service-log-content"), "尚无日志");
  pollOnce();
  restartPollTimer();
}

init();
