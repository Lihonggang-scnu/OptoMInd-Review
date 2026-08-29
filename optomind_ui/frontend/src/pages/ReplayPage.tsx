import { useCallback, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { EventLog, type LogRow } from "@/components/log/EventLog";
import { PageFrame } from "@/components/layout/PageFrame";
import { ThemeToggle } from "@/components/theme/ThemeToggle";
import { maskSensitive, useDemo } from "@/lib/demo";
import { STAGE_GROUPS, type StageGroup } from "@/lib/stageGroups.generated";

interface ReplayLine {
  ts: string;
  event: string;
  stage: string;
  stage_label: string;
  text: string;
  truncated?: boolean;
  withheld_fields?: number;
  withheld_bytes?: number;
}

interface ReplayMetricPoint {
  t: number;
  cost_cny: number;
  model_calls: number;
  stages_done: number;
}

interface ReplayMetricSnapshot {
  template_opener_share?: number;
  paragraph_opener_max_share?: number;
  while_paragraphs?: number;
  building_on_paragraphs?: number;
  strawman_not_but_before?: number;
  strawman_not_but_after?: number;
}

interface ReplayChapterSnapshot {
  section_id: string;
  title: string;
  status: string;
  changed: boolean;
  reviewer_edits_found: number;
  accepted_edits: number;
  rejected_edits: number;
}

interface ReplayStyleSnapshot {
  legacy?: {
    rewrites_attempted?: number;
    rewrites_accepted?: number;
    metrics_before?: ReplayMetricSnapshot;
    metrics_after?: ReplayMetricSnapshot;
  };
  chapter?: {
    chapters_attempted?: number;
    chapters_changed?: number;
    reviewer_calls?: number;
    reviser_calls?: number;
    estimated_cost_cny?: number;
    promotion_eligible?: boolean;
    promotion_reason?: string;
    improved?: boolean;
    metrics_before?: ReplayMetricSnapshot;
    metrics_after?: ReplayMetricSnapshot;
    chapters?: ReplayChapterSnapshot[];
    abbreviation_inventory?: Array<{
      abbreviation: string;
      full_form: string;
      full_form_mention_count: number;
      repeat_count_after_first_definition: number;
    }>;
  };
}

interface ReplayVisualRequest {
  section_id: string;
  figure_kind: string;
  priority: string;
  status: string;
  estimated_cost_cny: number;
}

interface ReplayVisualSnapshot {
  plan?: {
    placement_count?: number;
    conceptual_request_count?: number;
    unfilled_need_count?: number;
    requests?: ReplayVisualRequest[];
  };
  package?: {
    figure_count?: number;
    unfilled_visual_opportunities?: number;
    validation_status?: string;
  };
  cost?: {
    estimated_cost_cny?: number;
    image_generation_calls?: number;
    cache_hit_rate?: number;
    cache_namespace?: string;
  };
}

interface ReplaySnapshot {
  run?: {
    status?: string;
    current_stage?: string;
    error_count?: number;
    question?: string;
    cost_cny?: number;
    budget_cny?: number;
    remaining_budget_cny?: number;
  };
  style?: ReplayStyleSnapshot;
  visual?: ReplayVisualSnapshot;
  artifacts?: Array<{
    kind: string;
    label: string;
    available: boolean;
    embedded_in_static_bundle: boolean;
  }>;
}

interface ReplayData {
  meta: {
    name: string;
    generated_at: string;
    duration_seconds: number;
    event_count: number;
    merged_events: number;
    source_note: string;
  };
  timeline: ReplayLine[];
  metrics: ReplayMetricPoint[];
  snapshot?: ReplaySnapshot;
}

interface ReplayManifestEntry {
  id: string;
  label: string;
  short_label?: string;
  topic?: string;
  data_path: string;
  accent?: "cyan" | "violet" | "amber";
  status?: string;
  cost_cny?: number;
}

interface ReplayManifest {
  generated_at: string;
  runs: ReplayManifestEntry[];
}

type Tone = "positive" | "warning" | "danger" | "neutral";
type StepStatus = "pending" | "running" | "completed" | "degraded" | "failed" | "skipped";
type RailState = "pending" | "active" | "done" | "warn";

interface ReplayStep {
  stage: string;
  label: string;
  explain: string;
  status: StepStatus;
}

const ALL_STAGES: ReplayStep[] = STAGE_GROUPS.flatMap((group) =>
  group.stages.map((stage) => ({
    stage: stage.name,
    label: stage.label,
    explain: stage.explain,
    status: "pending" as StepStatus,
  })),
);

const ACCENT: Record<NonNullable<ReplayManifestEntry["accent"]>, string> = {
  cyan: "var(--replay-cyan)",
  violet: "var(--replay-violet)",
  amber: "var(--replay-amber)",
};

const EVENT_LABELS: Record<string, string> = {
  run_started: "运行开始",
  run_resumed: "运行恢复",
  run_finished: "运行完成",
  run_error: "运行异常",
  progress_note: "进度记录",
  paper_fetched: "论文获取",
  pdf_skipped: "PDF 生成跳过",
  stage_started: "阶段开始",
  stage_finished: "阶段完成",
  artifact_written: "产物写入",
  checkpoint_written: "断点保存",
  model_call_started: "模型调用开始",
  model_call_finished: "模型调用完成",
  error: "可恢复异常",
  warning: "运行提示",
  visual_contract_created: "视觉方案建立",
  final_visual_package_validated: "视觉包校验完成",
};

const INTERNAL_TOKEN_LABELS: Record<string, string> = {
  query_planner: "问题理解",
  topic_scoped_kb: "主题材料库",
  s2_literature_intelligence: "文献检索",
  review_lead: "结构设计",
  section_coverage: "证据整理",
  section_coverage_portfolio: "证据组合",
  phase3_argument_orchestration: "论点形成",
  authoring_revision: "章节初稿",
  section_coverage_feedback: "证据补充",
  publication_mainline_enhancement: "章节增强",
  publication_mainline_handoff: "全文交接",
  publication_mainline_commander: "全文编排",
  publication_mainline_staged_completion: "前后文补齐",
  article_completion: "论文收尾",
  article_structure_audit: "结构体检",
  visual_editor: "视觉规划",
  visual_materialization: "图表挂载",
  research_plan: "研究计划",
  packaging: "交付打包",
  latex_publication: "英文 PDF 编译",
  chinese_translation: "中文翻译",
  latex_publication_zh: "中文 PDF 编译",
  research_plan_publication: "计划附件",
  stage_started: "阶段开始",
  stage_finished: "阶段完成",
  final_text_only: "正文引用记录",
  conceptual_visual_unresolved: "概念图暂未生成",
  structured_diagram_fallback_skipped_by_budget: "结构图因预算暂缓",
  no_renderable_figures: "没有可渲染图片",
  pending_generation_and_review: "待生成与审核",
  completed_without_figures: "完成但无图片",
  changed_without_measurable_global_style_improvement: "章节有改动，但全局风格指标未提升",
};

const VISUAL_KIND_LABELS: Record<string, string> = {
  concept_map: "概念关系图",
  comparison_diagram: "对比关系图",
  mechanism_schematic: "机制示意图",
  workflow_schematic: "流程示意图",
  data_infographic: "数据信息图",
  trend_schematic: "趋势示意图",
  conceptual_diagram: "概念机制图",
  mechanism_diagram: "机制示意图",
  workflow: "流程图",
  comparison: "对比图",
  application: "应用示意图",
  taxonomy: "分类图",
  architecture: "系统架构图",
  plot: "数据图",
};

const RUN_TITLE_FALLBACKS: Array<[string, string]> = [
  ["optical_diffractive_neural_networks", "光学衍射神经网络"],
  ["metasurface_holography", "超表面全息"],
  ["photonic_computing", "规模化光子计算"],
];

function Icon({ name, size = 15 }: { name: string; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  switch (name) {
    case "arrow-left":
      return <svg {...common}><path d="m15 18-6-6 6-6" /><path d="M9 12h10" /></svg>;
    case "play":
      return <svg {...common}><path d="m8 5 11 7-11 7V5Z" /></svg>;
    case "pause":
      return <svg {...common}><path d="M8 5v14M16 5v14" /></svg>;
    case "rotate":
      return <svg {...common}><path d="M20 11a8 8 0 1 0 1 4" /><path d="M20 5v6h-6" /></svg>;
    case "activity":
      return <svg {...common}><path d="M3 12h4l2.2-7 4.2 14L16 12h5" /></svg>;
    case "layers":
      return <svg {...common}><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></svg>;
    case "wallet":
      return <svg {...common}><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4H19a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H6.5A2.5 2.5 0 0 1 4 17.5v-11Z" /><path d="M4 7h14a2 2 0 0 1 2 2v2h-5a2 2 0 0 0 0 4h5v2M15 13h.01" /></svg>;
    case "spark":
      return <svg {...common}><path d="m12 3 1.3 5.7L19 10l-5.7 1.3L12 17l-1.3-5.7L5 10l5.7-1.3L12 3Z" /><path d="m19 16 .6 2.4L22 19l-2.4.6L19 22l-.6-2.4L16 19l2.4-.6L19 16Z" /></svg>;
    case "file":
      return <svg {...common}><path d="M6 3h8l4 4v14H6V3Z" /><path d="M14 3v5h5M9 13h6M9 17h5" /></svg>;
    case "check":
      return <svg {...common}><path d="m5 12 4 4L19 6" /></svg>;
    default:
      return <svg {...common}><circle cx="12" cy="12" r="8" /></svg>;
  }
}

function formatClock(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const secs = safe % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function formatDuration(seconds?: number): string {
  if (seconds === undefined || !Number.isFinite(seconds)) return "—";
  const safe = Math.max(0, Math.round(seconds));
  if (safe >= 3600) return `${Math.floor(safe / 3600)} 小时 ${Math.floor((safe % 3600) / 60)} 分钟`;
  if (safe >= 60) return `${Math.floor(safe / 60)} 分钟 ${safe % 60} 秒`;
  return `${safe} 秒`;
}

function formatCount(value?: number): string {
  return value === undefined || !Number.isFinite(value) ? "—" : Math.round(value).toLocaleString("zh-CN");
}

function formatCurrency(value?: number): string {
  return value === undefined || !Number.isFinite(value) ? "—" : `¥${value.toFixed(2)}`;
}

function formatPercent(value?: number): string {
  return value === undefined || !Number.isFinite(value) ? "—" : `${(value * 100).toFixed(1)}%`;
}

function formatDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function cleanText(value: string | undefined, fallback = ""): string {
  if (!value || value.includes("�") || value.includes("����")) return fallback;
  return value;
}

function displaySection(value?: string): string {
  if (!value) return "全局";
  const match = value.match(/^S(\d+)$/i);
  if (match) return `第 ${Number(match[1])} 章`;
  if (/^section[_-]?\d+$/i.test(value)) return `第 ${value.match(/\d+/)?.[0] ?? ""} 章`;
  return value;
}

function displayStage(stage?: string, label?: string): string {
  if (stage && INTERNAL_TOKEN_LABELS[stage]) return INTERNAL_TOKEN_LABELS[stage];
  if (label && /[\u3400-\u9fff]/.test(label)) return label;
  return "研究步骤";
}

function displayEvent(value?: string): string {
  if (!value) return "研究事件";
  return EVENT_LABELS[value] ?? INTERNAL_TOKEN_LABELS[value] ?? "研究事件";
}

function displayReason(value?: string): string {
  if (!value) return "全局晋升指标未达到提升阈值";
  return EVENT_LABELS[value] ?? INTERNAL_TOKEN_LABELS[value] ?? "全局晋升指标未达到提升阈值";
}

function displayVisualKind(value?: string): string {
  if (!value) return "视觉机会";
  return VISUAL_KIND_LABELS[value] ?? "视觉示意图";
}

function displayPriority(value?: string): string {
  const normalized = String(value ?? "").toLowerCase();
  if (normalized.includes("high") || normalized === "p0") return "高";
  if (normalized.includes("low") || normalized === "p2") return "低";
  return "中";
}

function displayRunTitle(value?: string): string {
  const normalized = String(value ?? "").toLowerCase();
  return RUN_TITLE_FALLBACKS.find(([token]) => normalized.includes(token))?.[1] ?? "历史研究运行";
}

function displayLogText(value: string): string {
  let result = value;
  const labels = { ...EVENT_LABELS, ...INTERNAL_TOKEN_LABELS };
  for (const [token, label] of Object.entries(labels).sort(([a], [b]) => b.length - a.length)) {
    result = result.replace(new RegExp(`\\b${token}\\b`, "g"), label);
  }
  return result;
}

function statusLabel(value?: string): string {
  const normalized = String(value ?? "").toLowerCase();
  if (!normalized) return "未记录";
  if (normalized === "pending") return "未开始";
  if (normalized.includes("completed_without_figures")) return "完成但无图片";
  if (normalized.includes("pending_generation") || normalized.includes("pending_review")) return "待生成与审核";
  if (normalized.includes("awaiting") || normalized.includes("human_review")) return "等待复核";
  if (normalized.includes("submission_ready") || normalized === "ready") return "可交付";
  if (normalized.includes("degrad") || normalized.includes("partial")) return "降级完成";
  if (normalized.includes("fail") || normalized.includes("error")) return "失败";
  if (normalized.includes("completed") || normalized === "passed" || normalized === "success") return "完成";
  if (normalized.includes("running")) return "运行中";
  if (normalized.includes("skip")) return "已跳过";
  return "已记录";
}

function statusTone(value?: string): Tone {
  const normalized = String(value ?? "").toLowerCase();
  if (normalized.includes("fail") || normalized.includes("error")) return "danger";
  if (normalized.includes("degrad") || normalized.includes("awaiting") || normalized.includes("skip") || normalized.includes("partial")) return "warning";
  if (normalized.includes("completed") || normalized === "passed" || normalized === "success" || normalized.includes("ready")) return "positive";
  return "neutral";
}

function toneClass(tone: Tone): string {
  return tone === "neutral" ? "replay-chip" : `replay-chip replay-chip-${tone}`;
}

function normalizeStepStatus(value?: string): StepStatus {
  const normalized = String(value ?? "").toLowerCase();
  if (normalized.includes("fail") || normalized.includes("error")) return "failed";
  if (normalized.includes("degrad") || normalized.includes("partial")) return "degraded";
  if (normalized.includes("skip")) return "skipped";
  if (normalized.includes("start") || normalized.includes("running")) return "running";
  if (normalized.includes("complete") || normalized === "passed" || normalized === "success") return "completed";
  return "pending";
}

function countVisible(data: ReplayData, atSeconds: number): number {
  const firstTs = data.timeline[0]?.ts ?? "";
  if (!firstTs) return data.timeline.length;
  const t0 = Date.parse(firstTs);
  if (Number.isNaN(t0)) return data.timeline.length;
  const targetMs = t0 + atSeconds * 1000;
  let lo = 0;
  let hi = data.timeline.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    const ms = Date.parse(data.timeline[mid].ts);
    if (!Number.isNaN(ms) && ms <= targetMs) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function deriveSteps(timeline: ReplayLine[], visibleCount: number): ReplayStep[] {
  const statusByStage = new Map<string, StepStatus>();
  for (const line of timeline.slice(0, visibleCount)) {
    if (!line.stage) continue;
    if (line.event === "stage_started") statusByStage.set(line.stage, "running");
    else if (line.event === "stage_finished") statusByStage.set(line.stage, normalizeStepStatus(line.text));
  }
  return ALL_STAGES.map((step) => ({ ...step, status: statusByStage.get(step.stage) ?? "pending" }));
}

function stepForStage(stage: { name: string; label: string; explain: string }, steps: ReplayStep[]): ReplayStep {
  return steps.find((step) => step.stage === stage.name) ?? {
    stage: stage.name,
    label: stage.label,
    explain: stage.explain,
    status: "pending",
  };
}

function railState(steps: ReplayStep[]): RailState {
  if (steps.some((step) => step.status === "failed" || step.status === "degraded" || step.status === "skipped")) return "warn";
  if (steps.some((step) => step.status === "running")) return "active";
  if (steps.length > 0 && steps.every((step) => step.status === "completed")) return "done";
  if (steps.some((step) => step.status === "completed")) return "active";
  return "pending";
}

function resolvedCount(steps: ReplayStep[]): number {
  return steps.filter((step) => ["completed", "degraded", "failed", "skipped"].includes(step.status)).length;
}

function lastMetric(data?: ReplayData): ReplayMetricPoint | undefined {
  if (!data?.metrics.length) return undefined;
  return data.metrics[data.metrics.length - 1];
}

function currentMetric(data: ReplayData, t: number): ReplayMetricPoint | undefined {
  let found = data.metrics[0];
  for (const point of data.metrics) {
    if (point.t <= t + 1e-6) found = point;
    else break;
  }
  return found;
}

function sparkPoints(points: ReplayMetricPoint[], key: "cost_cny" | "model_calls" | "stages_done"): string {
  if (!points.length) return "0,22 100,22";
  const values = points.map((point) => Number(point[key]) || 0);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const spread = max - min || 1;
  return values.map((value, index) => {
    const x = values.length === 1 ? 50 : (index / (values.length - 1)) * 100;
    const y = 20 - ((value - min) / spread) * 17;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

function valueOrFallback(value: number | undefined, fallback: number | undefined): number | undefined {
  return value !== undefined ? value : fallback;
}

function percentWidth(value: number | undefined, maximum: number): string {
  if (value === undefined || !Number.isFinite(value) || maximum <= 0) return "0%";
  return `${Math.max(0, Math.min(100, (value / maximum) * 100))}%`;
}

function PanelHead({ kicker, title, subtitle, right }: { kicker: string; title: string; subtitle: string; right?: ReactNode }) {
  return (
    <div className="replay-panel-head">
      <div>
        <div className="replay-section-kicker">{kicker}</div>
        <h2 className="replay-panel-title">{title}</h2>
        <p className="replay-panel-subtitle">{subtitle}</p>
      </div>
      {right}
    </div>
  );
}

function StatusPill({ value, label }: { value?: string; label?: string }) {
  return <span className={toneClass(statusTone(value))}>{label ?? statusLabel(value)}</span>;
}

function ProgressRing({ value, label }: { value: number; label: string }) {
  const radius = 72;
  const circumference = 2 * Math.PI * radius;
  const progress = Math.max(0, Math.min(1, value));
  return (
    <div className="replay-hero-orbit" aria-label={`${label} ${Math.round(progress * 100)}%`}>
      <svg width="188" height="188" viewBox="0 0 188 188" aria-hidden="true">
        <circle cx="94" cy="94" r={radius} fill="none" stroke="rgba(145,177,211,.12)" strokeWidth="2" />
        <circle cx="94" cy="94" r={radius} fill="none" stroke="var(--replay-cyan)" strokeWidth="2.5" strokeLinecap="round" strokeDasharray={circumference} strokeDashoffset={circumference * (1 - progress)} transform="rotate(-90 94 94)" />
      </svg>
      <div className="replay-orbit-readout">
        <div className="replay-orbit-value">{Math.round(progress * 100)}%</div>
        <div className="replay-orbit-label">{label}</div>
      </div>
    </div>
  );
}

function KpiCard({ icon, label, value, note, accent, points }: { icon: string; label: string; value: string; note: string; accent: string; points?: string }) {
  return (
    <article className="replay-kpi" style={{ "--kpi-accent": accent } as CSSProperties}>
      <div className="replay-kpi-label"><Icon name={icon} size={14} /><span>{label}</span></div>
      <div className="replay-kpi-value">{value}</div>
      <div className="replay-kpi-note">
        <span>{note}</span>
        {points && <svg className="replay-sparkline" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><polyline points={points} fill="none" stroke={accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>}
      </div>
    </article>
  );
}

function RunDeck({ entries, activeId, onSelect, demo }: { entries: ReplayManifestEntry[]; activeId: string; onSelect: (id: string) => void; demo: boolean }) {
  return (
    <section aria-label="历史运行选择" className="replay-run-deck">
      {entries.map((entry, index) => {
        const accent = ACCENT[entry.accent ?? "cyan"];
        return (
          <button key={entry.id} type="button" aria-current={entry.id === activeId} className="replay-run-card" style={{ "--run-accent": accent } as CSSProperties} onClick={() => onSelect(entry.id)}>
            <span className="replay-run-card-top"><span className="replay-run-card-index">运行 {String(index + 1).padStart(2, "0")}</span><StatusPill value={entry.status} /></span>
            <span className="replay-run-card-name">{maskSensitive(entry.label, demo)}</span>
            <span className="replay-run-card-bottom"><span className="replay-run-card-topic">{maskSensitive(entry.topic ?? "静态运行资产", demo)}</span><span className="replay-run-card-cost">{formatCurrency(entry.cost_cny)}</span></span>
          </button>
        );
      })}
    </section>
  );
}

function StylePanel({ snapshot }: { snapshot?: ReplaySnapshot }) {
  const legacy = snapshot?.style?.legacy ?? {};
  const chapter = snapshot?.style?.chapter ?? {};
  const before = chapter.metrics_before ?? legacy.metrics_before ?? {};
  const after = chapter.metrics_after ?? legacy.metrics_after ?? {};
  const rows = [
    { label: "模板段首", before: before.template_opener_share, after: after.template_opener_share, formatter: formatPercent },
    { label: "While 段落", before: before.while_paragraphs, after: after.while_paragraphs, formatter: formatCount },
    { label: "Building on", before: before.building_on_paragraphs, after: after.building_on_paragraphs, formatter: formatCount },
    { label: "not-but 结构", before: before.strawman_not_but_before, after: after.strawman_not_but_after, formatter: formatCount },
  ];
  const abbreviations = chapter.abbreviation_inventory ?? [];
  const chapterRows = chapter.chapters ?? [];
  const maxFor = (row: { before?: number; after?: number }) => Math.max(row.before ?? 0, row.after ?? 0, 1);
  return (
    <section className="replay-panel" aria-label="风格治理摘要">
      <PanelHead kicker="质量 / 风格" title="章节风格治理" subtitle="逐章审稿与返修的实际运行信号；是否晋升全局由独立守门器决定。" right={<StatusPill value={chapter.improved ? "completed" : chapter.promotion_eligible ? "ready" : "degraded"} label={chapter.improved ? "已改善" : chapter.promotion_eligible ? "达到晋升条件" : "局部改善"} />} />
      <div className="replay-quality-grid">
        <div className="replay-quality-stat"><div className="replay-quality-label">审稿 / 返修</div><div className="replay-quality-value">{formatCount(chapter.reviewer_calls)} / {formatCount(chapter.reviser_calls)}</div><div className="replay-quality-hint">模型工作次数</div></div>
        <div className="replay-quality-stat"><div className="replay-quality-label">章节改动</div><div className="replay-quality-value">{formatCount(chapter.chapters_changed)}</div><div className="replay-quality-hint">{formatCount(chapter.chapters_attempted)} 章已处理</div></div>
        <div className="replay-quality-stat"><div className="replay-quality-label">旧治理接受</div><div className="replay-quality-value">{formatCount(legacy.rewrites_accepted)}</div><div className="replay-quality-hint">{formatCount(legacy.rewrites_attempted)} 次尝试</div></div>
        <div className="replay-quality-stat"><div className="replay-quality-label">治理成本</div><div className="replay-quality-value">{formatCurrency(chapter.estimated_cost_cny)}</div><div className="replay-quality-hint">章节级预算</div></div>
      </div>
      <div className="replay-before-after" style={{ marginTop: 20 }}>
        {rows.map((row) => (
          <div className="replay-before-after-row" key={row.label}>
            <span className="replay-before-after-label">{row.label}</span>
            <div className="replay-before-after-track" style={{ "--before-width": percentWidth(row.before, maxFor(row)), "--after-width": percentWidth(row.after, maxFor(row)) } as CSSProperties} title={`治理前 ${row.formatter(row.before)}，治理后 ${row.formatter(row.after)}`} />
            <span className="replay-before-after-value">{row.formatter(row.after)}</span>
          </div>
        ))}
      </div>
      <div className="replay-footnote" style={{ marginTop: 16 }}>灰条为治理前，青紫条为治理后 · {displayReason(chapter.promotion_reason)}</div>
      {(abbreviations.length > 0 || chapterRows.length > 0) && <div className="replay-abbreviation-list">
        {abbreviations.slice(0, 3).map((item) => <div className="replay-detail-row" key={item.abbreviation}><div className="replay-detail-row-main"><div className="replay-detail-row-title">{item.abbreviation} · {item.full_form}</div><div className="replay-detail-row-subtitle">全称出现 {formatCount(item.full_form_mention_count)} 次；定义后重复 {formatCount(item.repeat_count_after_first_definition)} 次</div></div><span className="replay-detail-row-value"><Icon name="check" size={13} /></span></div>)}
        {chapterRows.slice(0, 3).map((row) => <div className="replay-detail-row" key={row.section_id}><div className="replay-detail-row-main"><div className="replay-detail-row-title">{displaySection(row.section_id)} · {row.title}</div><div className="replay-detail-row-subtitle">审稿意见 {formatCount(row.reviewer_edits_found)} · 接受 {formatCount(row.accepted_edits)} · 拒绝 {formatCount(row.rejected_edits)}</div></div><span className="replay-detail-row-value">{row.changed ? "已改写" : "保留原文"}</span></div>)}
      </div>}
      {abbreviations.length === 0 && chapterRows.length === 0 && <div className="replay-empty-state">此运行未导出章节级明细。</div>}
    </section>
  );
}

function visualStatusTone(status?: string): Tone {
  const normalized = String(status ?? "").toLowerCase();
  if (normalized.includes("unresolved") || normalized.includes("skip") || normalized.includes("overflow") || normalized.includes("pending")) return "warning";
  if (normalized.includes("validated") || normalized.includes("hit") || normalized.includes("completed")) return "positive";
  return "neutral";
}

function VisualGlyph({ index, tone }: { index: number; tone: Tone }) {
  const stroke = tone === "positive" ? "var(--replay-green)" : tone === "warning" ? "var(--replay-amber)" : "var(--replay-cyan)";
  return <div className="replay-visual-glyph" aria-hidden="true"><svg viewBox="0 0 44 34" fill="none" stroke={stroke} strokeWidth="1.3">{index % 3 === 0 && <><path d="M4 26 14 9l8 11 6-8 12 14" /><path d="M7 29h31" /></>}{index % 3 === 1 && <><circle cx="22" cy="17" r="9" /><path d="M22 4v5M22 25v5M9 17h5M30 17h5M13 8l4 4M27 22l4 4" /></>}{index % 3 === 2 && <><rect x="6" y="7" width="32" height="20" rx="2" /><path d="M11 22 18 15l5 4 5-7 5 7" /></>}</svg></div>;
}

function VisualPanel({ snapshot }: { snapshot?: ReplaySnapshot }) {
  const visual = snapshot?.visual ?? {};
  const plan = visual.plan ?? {};
  const pack = visual.package ?? {};
  const cost = visual.cost ?? {};
  const requests = plan.requests ?? [];
  const remaining = pack.unfilled_visual_opportunities ?? plan.unfilled_need_count;
  return (
    <section className="replay-panel" aria-label="视觉链路摘要">
      <PanelHead kicker="资产链路 / 视觉" title="视觉资产雷达" subtitle="规划、挂载、生成预算与缓存隔离的静态审计摘要。" right={<StatusPill value={pack.validation_status} />} />
      <div className="replay-quality-grid">
        <div className="replay-quality-stat"><div className="replay-quality-label">最终图数</div><div className="replay-quality-value">{formatCount(pack.figure_count)}</div><div className="replay-quality-hint">已纳入文章</div></div>
        <div className="replay-quality-stat"><div className="replay-quality-label">未填需求</div><div className="replay-quality-value">{formatCount(remaining)}</div><div className="replay-quality-hint">待处理机会位</div></div>
        <div className="replay-quality-stat"><div className="replay-quality-label">概念图请求</div><div className="replay-quality-value">{formatCount(plan.conceptual_request_count)}</div><div className="replay-quality-hint">{formatCount(plan.placement_count)} 个已挂载</div></div>
        <div className="replay-quality-stat"><div className="replay-quality-label">生成成本</div><div className="replay-quality-value">{formatCurrency(cost.estimated_cost_cny)}</div><div className="replay-quality-hint">{formatCount(cost.image_generation_calls)} 次生成</div></div>
      </div>
      <div className="replay-visual-list">
        {requests.slice(0, 5).map((request, index) => {
          const tone = visualStatusTone(request.status);
          return <div className="replay-detail-row" key={`${request.section_id}-${request.figure_kind}-${index}`}><VisualGlyph index={index} tone={tone} /><div className="replay-detail-row-main"><div className="replay-detail-row-title">{displaySection(request.section_id)} · {displayVisualKind(request.figure_kind)}</div><div className="replay-detail-row-subtitle">优先级 {displayPriority(request.priority)} · 预算估算 {formatCurrency(request.estimated_cost_cny)}</div></div><span className={`replay-detail-row-value ${tone === "warning" ? "replay-visual-warning" : ""}`}>{statusLabel(request.status)}</span></div>;
        })}
      </div>
      <div className="replay-footnote" style={{ marginTop: 15 }}>{cost.cache_namespace ? "缓存隔离：按主题独立" : "缓存隔离信息未写入此回放包。"}{cost.cache_hit_rate !== undefined ? ` · 命中率 ${formatPercent(cost.cache_hit_rate)}` : ""}</div>
      {requests.length === 0 && <div className="replay-empty-state">此运行未导出视觉请求明细。</div>}
    </section>
  );
}

function ArtifactPanel({ snapshot }: { snapshot?: ReplaySnapshot }) {
  const artifacts = snapshot?.artifacts ?? [];
  return (
    <section className="replay-panel" aria-label="交付产物摘要">
      <PanelHead kicker="交付 / 产物" title="交付检查" subtitle="静态包只保留脱敏摘要；原始 PDF 与全文仍留在运行目录。" right={<Icon name="file" size={17} />} />
      <div className="replay-quality-grid">
        {artifacts.length > 0 ? artifacts.map((artifact) => <div className="replay-quality-stat" key={artifact.kind}><div className="replay-quality-label">{artifact.label}</div><div className="replay-quality-value"><Icon name={artifact.available ? "check" : "file"} size={15} /> {artifact.available ? "已生成" : "未生成"}</div><div className="replay-quality-hint">{artifact.embedded_in_static_bundle ? "已嵌入回放" : "原始目录保留"}</div></div>) : <div className="replay-empty-state">未记录交付产物。</div>}
      </div>
    </section>
  );
}

export function ReplayPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { demo, setDemo } = useDemo();
  const [manifest, setManifest] = useState<ReplayManifest | null>(null);
  const [dataState, setDataState] = useState<{ data: ReplayData | null; loading: boolean; error: Error | null }>({ data: null, loading: true, error: null });
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [focusedGroup, setFocusedGroup] = useState("");
  const requestedRunId = searchParams.get("run") ?? "";

  useEffect(() => {
    let alive = true;
    fetch("./replay-manifest.json", { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<ReplayManifest>;
      })
      .then((value) => {
        if (alive && Array.isArray(value.runs)) setManifest(value);
      })
      .catch(() => undefined);
    return () => { alive = false; };
  }, []);

  const selectedEntry = useMemo(() => {
    if (!manifest?.runs.length) return undefined;
    return manifest.runs.find((entry, index) => entry.id === requestedRunId || String(index + 1) === requestedRunId) ?? manifest.runs[0];
  }, [manifest, requestedRunId]);
  const dataPath = selectedEntry?.data_path ? `./${selectedEntry.data_path.replace(/^\.\//, "")}` : "./replay-data.json";

  useEffect(() => {
    let alive = true;
    setDataState({ data: null, loading: true, error: null });
    fetch(dataPath, { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<ReplayData>;
      })
      .then((data) => { if (alive) setDataState({ data, loading: false, error: null }); })
      .catch((cause) => { if (alive) setDataState({ data: null, loading: false, error: cause instanceof Error ? cause : new Error(String(cause)) }); });
    return () => { alive = false; };
  }, [dataPath]);

  const data = dataState.data;
  const duration = Math.max(data?.meta.duration_seconds ?? lastMetric(data ?? undefined)?.t ?? 0, 0);
  const visibleCount = useMemo(() => data ? countVisible(data, t) : 0, [data, t]);
  const rows: LogRow[] = useMemo(() => data ? data.timeline.slice(0, visibleCount).map((line, index) => ({
    key: `replay-${data.meta.name}-${index}`,
    ts: line.ts,
    event: line.event,
    stage_label: displayStage(line.stage, line.stage_label),
    text: displayLogText(line.text),
    truncated: line.truncated,
    withheld_fields: line.withheld_fields,
    withheld_bytes: line.withheld_bytes,
  })) : [], [data, visibleCount]);
  const steps = useMemo(() => data ? deriveSteps(data.timeline, visibleCount) : ALL_STAGES, [data, visibleCount]);
  const finalSteps = useMemo(() => data ? deriveSteps(data.timeline, data.timeline.length) : ALL_STAGES, [data]);
  const metric = data ? currentMetric(data, t) : undefined;
  const currentLine = data?.timeline[Math.max(0, visibleCount - 1)];
  const run = data?.snapshot?.run ?? {};
  const activeStep = steps.find((step) => step.status === "running") ?? (currentLine?.stage ? steps.find((step) => step.stage === currentLine.stage) : undefined) ?? (visibleCount >= (data?.timeline.length ?? 0) ? steps.find((step) => step.stage === run.current_stage) : undefined) ?? steps.find((step) => step.status === "pending") ?? steps[steps.length - 1];
  const activeGroup: StageGroup = STAGE_GROUPS.find((group) => group.stages.some((stage) => stage.name === activeStep?.stage)) ?? STAGE_GROUPS[0];

  useEffect(() => {
    if (data) {
      setT(0);
      setPlaying(false);
      setFocusedGroup("");
    }
  }, [data]);

  const selectedGroup = STAGE_GROUPS.find((group) => group.name === focusedGroup) ?? activeGroup;
  const selectedGroupSteps = selectedGroup.stages.map((stage) => stepForStage(stage, steps));
  const finalResolved = resolvedCount(finalSteps);
  const progress = ALL_STAGES.length ? finalResolved / ALL_STAGES.length : 0;
  const finalCost = valueOrFallback(run.cost_cny, lastMetric(data ?? undefined)?.cost_cny);
  const budget = run.budget_cny;
  const remaining = valueOrFallback(run.remaining_budget_cny, budget !== undefined && finalCost !== undefined ? budget - finalCost : undefined);
  const budgetProgress = budget && finalCost !== undefined ? Math.max(0, Math.min(1, finalCost / budget)) : 0;
  const visual = data?.snapshot?.visual;
  const question = cleanText(run.question, selectedEntry?.topic ?? "静态研究运行回放");
  const title = cleanText(selectedEntry?.short_label, selectedEntry?.label ?? displayRunTitle(data?.meta.name));
  const activeId = selectedEntry?.id ?? data?.meta.name ?? "";
  const accent = ACCENT[selectedEntry?.accent ?? "cyan"];

  const togglePlay = useCallback(() => {
    setPlaying((wasPlaying) => {
      if (!wasPlaying && duration > 0 && t >= duration) setT(0);
      return !wasPlaying;
    });
  }, [duration, t]);

  useEffect(() => {
    if (!playing || duration <= 0) return;
    const timer = window.setInterval(() => {
      setT((previous) => {
        const next = previous + 0.2 * speed;
        if (next >= duration) {
          setPlaying(false);
          return duration;
        }
        return next;
      });
    }, 200);
    return () => window.clearInterval(timer);
  }, [playing, speed, duration]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target?.tagName === "INPUT" || target?.tagName === "TEXTAREA" || target?.tagName === "SELECT") return;
      if (event.key === " ") {
        event.preventDefault();
        togglePlay();
      } else if (event.key === "ArrowLeft" && duration > 0) {
        event.preventDefault();
        setPlaying(false);
        setT((previous) => Math.max(0, previous - 5));
      } else if (event.key === "ArrowRight" && duration > 0) {
        event.preventDefault();
        setPlaying(false);
        setT((previous) => Math.min(duration, previous + 5));
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [duration, togglePlay]);

  function selectRun(id: string) {
    setPlaying(false);
    setT(0);
    const index = manifest?.runs.findIndex((entry) => entry.id === id) ?? -1;
    setSearchParams(index >= 0 ? { run: String(index + 1) } : {}, { replace: true });
  }

  return (
    <PageFrame crumbs={[{ label: "静态回放" }]} immersive>
      <div className="replay-shell replay-noise" style={{ "--run-accent": accent } as CSSProperties}>
        <div className="replay-content">
          <header className="replay-topbar">
            <div className="replay-inline-actions">
              <button type="button" className="replay-icon-button" onClick={() => navigate(-1)} aria-label="返回上一页" title="返回上一页"><Icon name="arrow-left" /></button>
              <div className="replay-brand"><span className="replay-brand-mark">OM</span><span><span className="replay-brand-name">OptoMind</span><span className="replay-brand-meta"> · 研究运行观察台</span></span></div>
            </div>
            <div className="replay-nav-actions"><span className="replay-chip replay-chip-cyan"><span aria-hidden>●</span> 静态只读</span><button type="button" className="replay-quiet-button" aria-pressed={demo} onClick={() => setDemo(!demo)}>演示模式 {demo ? "开" : "关"}</button><ThemeToggle /></div>
          </header>

          {dataState.loading && <div className="replay-skeleton" aria-busy="true" aria-label="加载回放数据" />}
          {dataState.error && <div className="replay-error" role="alert"><div className="replay-error-title">回放数据暂时不可用</div><div className="replay-error-text">{dataState.error.message}。请通过本地 HTTP 服务打开回放目录；直接用 file:// 打开时，浏览器会阻止 JSON 读取。</div><button type="button" className="replay-error-action" onClick={() => window.location.reload()}>重新加载</button></div>}

          {data && <>
            <section className="replay-hero">
              <div><div className="replay-eyebrow">OptoMind / 研究回放 / 质量审查</div><h1 className="replay-hero-title">{maskSensitive(title, demo)}</h1><p className="replay-hero-question">{maskSensitive(question, demo)}</p><div className="replay-hero-meta"><StatusPill value={run.status} /><span className="replay-chip">{formatDate(data.meta.generated_at)}</span><span className="replay-chip">运行时长 {formatDuration(data.meta.duration_seconds)}</span><span className="replay-chip">事件 {formatCount(data.meta.event_count)} 条</span></div></div>
              <ProgressRing value={progress} label="流程已解析" />
            </section>

            <RunDeck entries={manifest?.runs ?? [{ id: activeId, label: title, short_label: title, topic: question, data_path: "replay-data.json", status: run.status, cost_cny: finalCost, accent: selectedEntry?.accent ?? "cyan" }]} activeId={activeId} onSelect={selectRun} demo={demo} />

            <section className="replay-kpi-grid" aria-label="运行关键指标">
              <KpiCard icon="activity" label="运行状态" value={statusLabel(run.status)} note={`${formatCount(run.error_count)} 条异常记录`} accent="var(--replay-cyan)" points={sparkPoints(data.metrics, "stages_done")} />
              <KpiCard icon="wallet" label="累计成本" value={formatCurrency(metric?.cost_cny ?? finalCost)} note={`剩余预算 ${formatCurrency(remaining)}`} accent="var(--replay-violet)" points={sparkPoints(data.metrics, "cost_cny")} />
              <KpiCard icon="layers" label="阶段解析" value={`${formatCount(metric?.stages_done ?? finalResolved)} / ${ALL_STAGES.length}`} note={`最终已解析 ${finalResolved} 个阶段`} accent="var(--replay-green)" points={sparkPoints(data.metrics, "stages_done")} />
              <KpiCard icon="spark" label="视觉就绪" value={formatCount(visual?.package?.figure_count)} note={`${formatCount(visual?.package?.unfilled_visual_opportunities ?? visual?.plan?.unfilled_need_count)} 个机会位待处理`} accent="var(--replay-amber)" />
            </section>

            <section className="replay-grid-main">
              <section className="replay-panel" aria-label="研究流水线阶段">
                <PanelHead kicker={`流程 / ${ALL_STAGES.length} 个阶段`} title="研究运行轨道" subtitle="点击阶段组查看当前回放光标下的细分状态。" right={<span className="replay-chip">模型调用 {formatCount(metric?.model_calls)} 次</span>} />
                <div className="replay-stage-rail" style={{ "--run-accent": accent } as CSSProperties}>{STAGE_GROUPS.map((group, index) => { const groupSteps = group.stages.map((stage) => stepForStage(stage, steps)); const state = railState(groupSteps); return <button key={group.name} type="button" className="replay-stage-node" data-state={state} aria-current={selectedGroup.name === group.name} onClick={() => setFocusedGroup(group.name)}><span className="replay-stage-dot">{state === "done" ? <Icon name="check" size={13} /> : String(index + 1).padStart(2, "0")}</span><span className="replay-stage-name">{group.name}</span><span className="replay-stage-count">{resolvedCount(groupSteps)}/{groupSteps.length}</span></button>; })}</div>
                <div className="replay-stage-detail" style={{ "--run-accent": accent } as CSSProperties}><span className="replay-stage-detail-index">{String(STAGE_GROUPS.indexOf(selectedGroup) + 1).padStart(2, "0")}</span><div><div className="replay-stage-detail-name">{selectedGroup.name} / {selectedGroupSteps.find((step) => step.status === "running")?.label ?? selectedGroupSteps.find((step) => step.stage === activeStep?.stage)?.label ?? selectedGroupSteps[0]?.label}</div><div className="replay-stage-detail-explain">{selectedGroupSteps.find((step) => step.status === "running")?.explain ?? selectedGroupSteps.find((step) => step.stage === activeStep?.stage)?.explain ?? selectedGroupSteps[0]?.explain}</div></div><span className="replay-stage-status">{statusLabel(selectedGroupSteps.find((step) => step.status === "running")?.status ?? selectedGroupSteps.find((step) => step.stage === activeStep?.stage)?.status)}</span></div>
                <div className="replay-footnote" style={{ marginTop: 16 }}>{formatCount(finalResolved)} / {ALL_STAGES.length} 个阶段已解析 · 当前光标 {formatCount(visibleCount)} / {formatCount(data.timeline.length)} 条事件 · 状态 {statusLabel(run.status)}</div>
              </section>

              <section className="replay-panel replay-now" aria-label="当前回放事件">
                <PanelHead kicker="回放光标 / 当前详情" title="当前光标" subtitle="回放进度会同步刷新日志、阶段和指标。" />
                <div className="replay-now-card"><div className="replay-now-label">{playing ? "正在回放" : "已暂停"}</div><div className="replay-now-event">{currentLine?.stage ? displayStage(currentLine.stage, currentLine.stage_label) : currentLine ? displayEvent(currentLine.event) : activeStep?.label || "等待第一条事件"}</div><div className="replay-now-text">{displayLogText(currentLine?.text ?? "拖动底部时间轴，查看运行从问题理解到 PDF 交付的决策轨迹。")}</div><div className="replay-now-foot"><span>{displayEvent(currentLine?.event)}</span><span>{currentLine?.ts ? formatDate(currentLine.ts) : "—"}</span></div></div>
                <div className="replay-signal-list" style={{ marginTop: 18 }}><div className="replay-signal-row"><span className="replay-signal-label">预算消耗</span><span className="replay-signal-value">{formatPercent(budgetProgress)}</span><div className="replay-signal-track"><div className="replay-signal-fill" style={{ "--signal-accent": "var(--replay-violet)", width: `${budgetProgress * 100}%` } as CSSProperties} /></div></div><div className="replay-signal-row"><span className="replay-signal-label">流程完成</span><span className="replay-signal-value">{formatPercent(progress)}</span><div className="replay-signal-track"><div className="replay-signal-fill" style={{ "--signal-accent": "var(--replay-cyan)", width: `${progress * 100}%` } as CSSProperties} /></div></div></div>
              </section>
            </section>

            <section className="replay-grid-secondary"><StylePanel snapshot={data.snapshot} /><VisualPanel snapshot={data.snapshot} /></section>
            <section style={{ marginTop: 14 }}><ArtifactPanel snapshot={data.snapshot} /></section>
            <section className="replay-panel replay-activity" aria-label="事件活动流"><PanelHead kicker="追踪 / 事件流" title="活动流" subtitle="事件正文经过脱敏和大小限制；展开能力只在在线运行详情页可用。" right={<span className="replay-chip">已显示 {formatCount(rows.length)} / 共 {formatCount(data.timeline.length)} 条</span>} /><div className="replay-log-wrap"><EventLog key={data.meta.name} rows={rows} runId={`replay:${data.meta.name}`} heightPx={360} /></div></section>
          </>}
        </div>

        {data && <div className="replay-transport" aria-label="回放控制"><div className="replay-transport-inner"><div className="replay-transport-controls"><button type="button" className="replay-play-button" onClick={togglePlay} aria-label={playing ? "暂停回放" : "播放回放"} title={playing ? "暂停（空格）" : "播放（空格）"}><Icon name={playing ? "pause" : "play"} size={16} /><span className="sr-only">{playing ? "暂停" : "播放"}</span></button><button type="button" className="replay-icon-button" onClick={() => { setPlaying(false); setT(0); }} aria-label="回到开头" title="回到开头"><Icon name="rotate" size={14} /></button></div><div className="replay-range-wrap" style={{ "--range-progress": `${duration > 0 ? (t / duration) * 100 : 0}%` } as CSSProperties}><input type="range" min={0} max={Math.max(duration, 0.1)} step={0.1} value={t} aria-label="回放进度" onChange={(event) => { setPlaying(false); setT(Number(event.target.value)); }} /></div><div className="replay-transport-meta"><span className="replay-transport-time">{formatClock(t)} <span>/ {formatClock(duration)}</span></span><div className="replay-speed-group" aria-label="回放速度">{[0.5, 1, 2, 4].map((value) => <button key={value} type="button" className="replay-speed-button" aria-pressed={speed === value} onClick={() => setSpeed(value)}>{value}×</button>)}</div></div></div></div>}
      </div>
    </PageFrame>
  );
}
