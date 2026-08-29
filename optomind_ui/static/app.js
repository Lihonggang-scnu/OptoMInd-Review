"use strict";
const $ = (selector) => document.querySelector(selector);
let currentRun = null;
let refreshTimer = null;
let refreshPaused = false;
let eventsState = { offset: 0, total: 0 };

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}
function esc(value) { const node = document.createElement("div"); node.textContent = value == null ? "" : String(value); return node.innerHTML; }
function show(view) { document.querySelectorAll(".view").forEach((node) => node.classList.add("hidden")); $("#view-" + view).classList.remove("hidden"); }
function card(label, value) { return "<div class='card'><span>" + esc(label) + "</span><strong>" + value + "</strong></div>"; }

async function renderRuns() {
  show("home");
  const runs = await api("/api/runs");
  if (!runs.length) { $("#runs-body").innerHTML = "<p class='empty'>还没有研究任务。输入一个问题，OptoMind 会从这里开始。</p>"; return; }
  $("#runs-body").innerHTML = "<div class='run-list'>" + runs.slice(0, 8).map((run) => { const title = run.question || "未记录问题的历史任务"; const stage = run.current_label || "准备中"; const status = run.status_label || "历史任务"; return "<button class='run-row' data-run='" + esc(run.run_id) + "'><span><b>" + esc(title) + "</b><small>" + esc(stage) + " · " + esc(run.error_count ? (run.error_count + " 个待处理问题") : "运行记录已保存") + "</small></span><span class='run-state'>" + esc(status) + "</span></button>"; }).join("") + "</div>";
  document.querySelectorAll(".run-row").forEach((button) => button.addEventListener("click", () => renderTask(button.dataset.run, "")));
}
function stageIcon(status) { return status === "completed" ? "✓" : status === "running" ? "●" : ""; }
function renderProgress(progress) {
  const completed = progress.steps.filter((step) => step.status === "completed").length;
  const percent = Math.round((completed / Math.max(1, progress.steps.length)) * 100);
  $("#progress-line").style.width = percent + "%";
  $("#progress-steps").innerHTML = progress.steps.map((step) => "<div class='progress-step " + step.status + "'><span class='step-icon'>" + stageIcon(step.status) + "</span><span>" + esc(step.label) + "</span></div>").join("");
  $("#task-status").textContent = progress.current_label || progress.status || "准备中";
  $("#metric-status").textContent = progress.status || "—";
  $("#metric-cost").textContent = "¥" + Number(progress.cost_cny || 0).toFixed(3);
  $("#metric-stage").textContent = progress.current_label || "—";
  $("#metric-errors").textContent = String(progress.error_count || 0);
}
async function renderTask(runId, question) {
  currentRun = runId; show("task");
  const stopButton = $("#stop-task"); if (stopButton) stopButton.classList.remove("hidden"); $("#task-question").textContent = question || "正在读取你的研究问题…"; $("#open-run").href = "#/run/" + encodeURIComponent(runId);
  clearInterval(refreshTimer);
  const update = async () => {
    try { const progress = await api("/api/tasks/" + encodeURIComponent(runId) + "/progress"); if (progress.question) $("#task-question").textContent = progress.question; renderProgress(progress); $("#task-status").textContent = progress.status_label || progress.current_label || "准备中"; const log = await api("/api/tasks/" + encodeURIComponent(runId) + "/log?tail=120"); $("#task-log").innerHTML = log.lines.map((line) => "<div>" + esc(line) + "</div>").join("") || "<div class='muted'>正在等待后台返回第一条日志…</div>"; $("#log-caption").textContent = log.lines.length ? (log.lines.length + " 条记录") : "等待第一条日志"; if (["completed", "failed", "needs_model_recovery", "budget_exhausted", "awaiting_human_review"].includes(progress.status)) clearInterval(refreshTimer); } catch (error) { $("#task-log").innerHTML = "<div class='bad'>读取任务状态失败：" + esc(error.message) + "</div>"; }
  };
  await update();
  refreshTimer = setInterval(() => { if (!refreshPaused) update(); }, 2000);
}
async function createTask(event) {
  event.preventDefault(); const input = $("#question"); const error = $("#form-error"); const question = input.value.trim(); error.classList.add("hidden"); if (question.length < 4) { error.textContent = "请先写一个完整的科研问题。"; error.classList.remove("hidden"); return; } const button = $("#submit-task"); button.disabled = true; button.classList.add("loading"); try { const task = await api("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) }); await renderTask(task.run_id, question); } catch (err) { error.textContent = err.message; error.classList.remove("hidden"); } finally { button.disabled = false; button.classList.remove("loading"); }
}
async function renderRun(runId) {
  currentRun = runId; show("run"); $("#run-title").textContent = runId; const detail = await api("/api/runs/" + encodeURIComponent(runId)); $("#run-cards").innerHTML = card("状态", esc(detail.status)) + card("当前阶段", esc(detail.current_stage || "—")) + card("费用", "¥" + Number(detail.cost_cny || 0).toFixed(3)) + card("错误数", esc(detail.error_count || 0)); $("#run-links").innerHTML = "<a class='quiet-button' href='#/events/" + encodeURIComponent(runId) + "'>事件流</a><a class='quiet-button' href='#/visuals/" + encodeURIComponent(runId) + "'>视觉产物</a><a class='quiet-button' href='#/decisions/" + encodeURIComponent(runId) + "'>人在回路</a>"; $("#timeline-table tbody").innerHTML = (detail.timeline || []).map((row) => "<tr><td>" + esc(row.stage) + "</td><td>" + esc(row.wall_time_seconds || 0) + " 秒</td></tr>").join(""); try { const data = await api("/api/runs/" + encodeURIComponent(runId) + "/deliverables"); $("#deliverables-body").innerHTML = "<p>交付状态：" + esc(data.gate_status) + "</p>" + (data.deliverables || []).map((item) => "<div class='deliverable-row'><b>" + esc(item.name) + "</b><span>" + esc(item.status || (item.ok ? "通过" : "待处理")) + "</span></div>").join(""); } catch (error) { $("#deliverables-body").textContent = error.message; }
}
async function renderEvents(runId, offset = 0) { currentRun = runId; show("events"); const page = await api("/api/runs/" + encodeURIComponent(runId) + "/events?offset=" + offset + "&limit=200"); eventsState = { offset, total: page.total_lines }; $("#ev-pos").textContent = (offset + 1) + "-" + (offset + page.returned) + " / " + page.total_lines; $("#events-body").innerHTML = page.events.map((item) => "<pre class='event-line'>" + esc(JSON.stringify(item)) + "</pre>").join(""); }
async function renderVisuals(runId) { currentRun = runId; show("visuals"); const data = await api("/api/runs/" + encodeURIComponent(runId) + "/visuals"); $("#visuals-body").innerHTML = data.available ? (data.delivered_figures || []).map((fig) => "<div class='deliverable-row'><b>" + esc(fig.figure_id) + " · " + esc(fig.section_id) + "</b><span>已交付</span></div>").join("") + (data.blocked_opportunities || []).map((item) => "<div class='deliverable-row'><b>" + esc(item.section_id) + " · 未填充</b><span>" + esc(item.reason) + "</span></div>").join("") : "本次运行尚未生成视觉包。"; }
async function renderDecisions(runId) { currentRun = runId; show("decisions"); const data = await api("/api/runs/" + encodeURIComponent(runId) + "/decisions"); $("#decisions-body").innerHTML = (data.pending || []).map((item) => "<div class='decision'><b>" + esc(item.kind) + " · " + esc(item.subject_id) + "</b><p>等待人工选择：" + esc((item.options || []).join(" / ")) + "</p></div>").join("") || "当前没有等待人工的决策。"; }
function route() { const parts = (location.hash || "#/home").slice(2).split("/"); if (parts[0] === "run" && parts[1]) return renderRun(parts[1]); if (parts[0] === "events" && parts[1]) return renderEvents(parts[1], eventsState.offset); if (parts[0] === "visuals" && parts[1]) return renderVisuals(parts[1]); if (parts[0] === "decisions" && parts[1]) return renderDecisions(parts[1]); if (parts[0] === "task" && parts[1]) return renderTask(parts[1], ""); return renderRuns(); }
$("#task-form").addEventListener("submit", createTask); $("#refresh-runs").addEventListener("click", renderRuns); $("#new-task").addEventListener("click", renderRuns); $("#back-home").addEventListener("click", renderRuns); $("#events-home").addEventListener("click", renderRuns); $("#stop-task").addEventListener("click", async () => {
  if (!currentRun) return;
  if (!confirm("确定要停止当前研究吗？此操作不可撤销，已产生的费用不予退还。")) return;
  try { await api("/api/tasks/" + encodeURIComponent(currentRun) + "/stop", { method: "POST" }); $("#task-status").textContent = "已由你停止"; }
  catch (error) { alert("停止失败：" + error.message); }
});
$("#stop-refresh").addEventListener("click", () => { refreshPaused = !refreshPaused; $("#stop-refresh").textContent = refreshPaused ? "继续刷新" : "暂停刷新"; }); $("#ev-prev").addEventListener("click", () => renderEvents(currentRun, Math.max(0, eventsState.offset - 200))); $("#ev-next").addEventListener("click", () => renderEvents(currentRun, eventsState.offset + 200)); window.addEventListener("hashchange", route); route();
