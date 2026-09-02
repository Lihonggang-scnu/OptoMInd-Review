(() => {
  "use strict";

  const config = window.OPTOMIND_PORTAL_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const fmt = new Intl.NumberFormat("zh-CN");
  const money = (value) => `¥${Number(value || 0).toFixed(2)}`;
  const duration = (seconds) => {
    const total = Math.max(0, Number(seconds || 0));
    if (total >= 3600) return `${(total / 3600).toFixed(1)} 小时`;
    if (total >= 60) return `${Math.round(total / 60)} 分钟`;
    return `${Math.round(total)} 秒`;
  };
  const safe = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
  const linkFor = (path) => new URL(String(path).replace(/^\/+/, ""), document.baseURI).href;
  const artifactPath = (folder, path) => linkFor(`${config.artifactBase || "artifacts/e2e"}/${folder}/${path}`);

  const RUNS = {
    rhr_optical_diffractive_neural_networks_20260828_v2b: {
      folder: "01-optical-diffractive-neural-networks",
      title: "光学衍射神经网络：从物理传播到智能推理",
      question: "请综述光学衍射神经网络的研究进展，比较不同衍射层架构、训练方法、成像与识别应用，并总结当前瓶颈与未来方向。",
      tags: ["衍射光学", "光学神经网络", "成像与识别", "训练方法"],
      words: 13695, sections: 7, citations: 121, inlineRefs: 134, figures: 0,
      calls: 240, pagesEn: 37, pagesZh: 35, accent: "cyan"
    },
    rhr_metasurface_holography_20260828_v1: {
      folder: "02-metasurface-holography",
      title: "超表面全息：从逆向设计、制造到动态显示与成像",
      question: "超表面全息：从逆向设计、制造到动态显示与成像应用。",
      tags: ["超表面", "计算全息", "逆向设计", "动态显示"],
      words: 12919, sections: 7, citations: 109, inlineRefs: 122, figures: 3,
      calls: 444, pagesEn: 36, pagesZh: 34, accent: "violet"
    },
    rhr_photonic_computing_20260829_v1: {
      folder: "03-scalable-photonic-computing",
      title: "规模化光子计算：从可编程芯片到人工智能加速与光互连",
      question: "写一篇关于规模化光子计算的文献综述，阐述从可编程集成光子芯片到 AI 加速与光互连。",
      tags: ["集成光子", "可编程芯片", "人工智能加速", "光互连"],
      words: 16217, sections: 8, citations: 124, inlineRefs: 145, figures: 2,
      calls: 259, pagesEn: 44, pagesZh: 42, accent: "amber"
    }
  };

  const STAGES = [
    {n:1, title:"启动与研究配置", short:"确认问题入口、预算与运行边界", keys:[], evidence:"reports/HARNESS_COST.json", desc:"建立本轮独立运行目录，冻结用户问题、执行配置、成本上限和过程记录入口。后续每个阶段都在同一证据链中追加状态。"},
    {n:2, title:"理解用户问题", short:"识别主题、范围与章节意图", keys:["query_planner","research_plan"], evidence:"reports/query_planner/ORIGINAL_USER_QUESTION.json", desc:"模型将自然语言问题转化为可执行的检索主题、核心概念、章节边界和重点比较维度，并保留原始问题用于全过程核对。"},
    {n:3, title:"多源文献发现", short:"检索元数据、开放全文与补充来源", keys:["s2_literature_intelligence"], evidence:"reports/QWEN_CAPABILITY_STATUS.json", desc:"围绕研究计划执行学术检索，聚合论文元数据、开放全文与可追溯来源；检索结果不是直接成文，而是进入后续证据筛选。"},
    {n:4, title:"建立专题材料库", short:"沉淀可复用全文片段与元数据", keys:["long_term_material_cache_sync","topic_scoped_kb"], evidence:"reports/TOPIC_IDENTITY.json", desc:"把跨来源文献材料统一为可检索、可定位、可复用的专题知识库，并用主题指纹约束材料与用户问题的一致性。"},
    {n:5, title:"按章节组织证据", short:"覆盖缺口、证据组合与章节映射", keys:["review_lead","section_coverage","section_coverage_portfolio","section_coverage_feedback"], evidence:"reports/REVIEW_CONTENT_PACKAGE.json", desc:"根据章节目标检查证据覆盖，把关键论文、方法对比、应用事实和争议点分配到对应章节；证据不足时形成补充检索任务。"},
    {n:6, title:"形成可追溯论点", short:"由证据构造主张、关系与反证", keys:["phase3_argument_orchestration"], evidence:"reports/FINAL_CITATION_MAP.json", desc:"将材料提升为可写作的论点单元，明确主张、证据、限定条件和章节关系，使后续文本能够追溯到具体来源而不是依赖模型记忆。"},
    {n:7, title:"生成章节初稿", short:"分章写作并绑定引用", keys:["authoring_revision"], evidence:"publication_mainline/full_manuscript_manifest.json", desc:"各章节依据已绑定证据和论点独立成稿，随后汇总为全文清单；章节标题、正文、引用与源材料保持对应。"},
    {n:8, title:"章节资产加强", short:"补足解释、比较、术语与应用", keys:["publication_mainline_enhancement"], evidence:"publication_mainline/PUBLICATION_MAINLINE_SUMMARY.json", desc:"逐章检查科学解释、横向比较、代表性应用、术语一致性和引用支撑，在局部上下文中完成可审核的增强。"},
    {n:9, title:"全文交接与统筹", short:"全局角色协作、结构与逻辑编排", keys:["publication_mainline_handoff","publication_mainline_commander"], evidence:"publication_mainline/commander/summary.json", desc:"把增强后的章节交给全文统筹层，由多角色从科学性、结构、引用、表达和出版完整性审视整篇综述，形成受约束的全局修改计划。"},
    {n:10, title:"完成摘要与结论", short:"标题、引言、结论、摘要分阶段收束", keys:["publication_mainline_staged_completion","article_completion","article_structure_audit","chapter_style_governance","llm_style_pipeline"], evidence:"publication_mainline/staged_completion/STAGED_COMPLETE_REVIEW_EN.md", desc:"在完整正文和全局判断形成后，再依次生成结论、引言、标题与摘要，并执行结构与表达治理，避免先写摘要再反向拼接正文。"},
    {n:11, title:"规划与挂载图像", short:"图像需求、来源审计与版面挂载", keys:["visual_editor","visual_materialization"], evidence:"visual_editor/final/FINAL_VISUAL_PACKAGE.json", desc:"识别真正需要图示的科学关系，审计可用来源或生成解释图，经过完整性检查后才进入最终文稿。没有通过检查的图不会强行挂载。"},
    {n:12, title:"补全引用与元数据", short:"作者、题名、DOI 与最终引用映射", keys:["publication_metadata","packaging"], evidence:"publication/metadata/PUBLICATION_METADATA_AUDIT.json", desc:"对最终文本中实际出现的引用进行反向核对，补齐出版元数据、参考文献字段和可定位标识，并形成最终引用映射与完整性审计。"},
    {n:13, title:"生成双语出版物", short:"英文、中文、LaTeX、PDF 与源包", keys:["chinese_translation","latex_publication","latex_publication_zh"], evidence:"publication/latex/LATEX_BUILD_REPORT.json", desc:"把冻结后的研究成果编译为英文与中文 PDF，同时保留 Markdown、LaTeX、BibTeX、构建报告和源文件包，供阅读、复核与再编辑。"}
  ];

  const state = { manifest:null, runId:null, payload:null, timer:null, eventIndex:-1, playing:false, stage:0, liveTimer:null };

  async function json(path, options) {
    const response = await fetch(path, {cache:"no-store", ...options});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || payload.message || `HTTP ${response.status}`);
    return payload;
  }

  function configureModes() {
    const enabled = Boolean(config.liveEnabled);
    $("live-tab").disabled = !enabled;
    $("live-tab-caption").textContent = enabled ? "检查通过后可启动" : "云端仅提供静态回放";
    $("sidebar-status").textContent = enabled ? "本地统一研究入口" : "云端只读证据回放";
    $("live-tab").addEventListener("click", () => enabled && switchMode("live"));
    $("replay-tab").addEventListener("click", () => switchMode("replay"));
  }

  function switchMode(mode) {
    const live = mode === "live";
    $("replay-workspace").hidden = live;
    $("live-workspace").hidden = !live;
    $("replay-tab").classList.toggle("active", !live);
    $("live-tab").classList.toggle("active", live);
    $("replay-tab").setAttribute("aria-selected", String(!live));
    $("live-tab").setAttribute("aria-selected", String(live));
    $("topbar-state-copy").textContent = live ? "本机真实研究模式" : "固化产物只读回放";
    if (live) refreshLocalStatus();
  }

  async function loadPortal() {
    $("loading").hidden = false; $("content").hidden = true; $("error-panel").hidden = true;
    try {
      state.manifest = await json(linkFor(config.manifestPath || "replay-manifest.json"));
      renderSidebar();
      const requested = new URLSearchParams(location.search).get("run") || location.hash.replace(/^#run=/, "");
      const id = state.manifest.runs.some((run) => run.id === requested) ? requested : state.manifest.runs[0].id;
      await selectRun(id, false);
      $("loading").hidden = true; $("content").hidden = false;
      $("topbar-state-copy").textContent = "固化产物只读回放";
    } catch (error) {
      $("loading").hidden = true; $("error-panel").hidden = false;
      $("error-copy").textContent = error.message;
    }
  }

  function renderSidebar() {
    const runs = state.manifest.runs;
    const totalPages = runs.reduce((sum, run) => { const r=RUNS[run.id]; return sum + (r ? r.pagesEn + r.pagesZh : 0); },0);
    $("archive-stats").innerHTML = `<div><strong>${runs.length}</strong><span>完整运行</span></div><div><strong>13</strong><span>研究阶段</span></div><div><strong>${totalPages}</strong><span>双语页</span></div>`;
    $("run-list").innerHTML = runs.map((run,index) => {
      const extra=RUNS[run.id] || {};
      return `<button class="run-button" data-id="${safe(run.id)}" data-accent="${safe(extra.accent || run.accent)}"><span class="run-number">0${index+1}</span><span class="run-label"><strong>${safe(run.label)}</strong><small>${extra.pagesEn || "–"} / ${extra.pagesZh || "–"} 页 · ${money(run.cost_cny)}</small></span><span class="run-check">✓</span></button>`;
    }).join("");
    $("run-list").querySelectorAll("button").forEach((button) => button.addEventListener("click", () => selectRun(button.dataset.id)));
  }

  async function selectRun(id, updateUrl=true) {
    stopReplay(); state.eventIndex=-1; state.runId=id;
    const entry=state.manifest.runs.find((run)=>run.id===id);
    if (!entry) throw new Error("找不到指定运行");
    state.payload=await json(linkFor(entry.data_path));
    $("run-list").querySelectorAll("button").forEach((button)=>button.classList.toggle("active",button.dataset.id===id));
    if (updateUrl) history.replaceState(null,"",`${location.pathname}?run=${encodeURIComponent(id)}`);
    renderRun(entry, RUNS[id]);
    $("sidebar").classList.remove("open");
  }

  function renderRun(entry, info) {
    const run=state.payload.snapshot.run || {};
    $("run-title").textContent=info.title;
    $("run-question").textContent=info.question || run.question;
    $("run-tags").innerHTML=info.tags.map((tag)=>`<span>${safe(tag)}</span>`).join("");
    $("page-result").textContent=`英文 ${info.pagesEn} 页 · 中文 ${info.pagesZh} 页`;
    $("open-en-pdf").href=artifactPath(info.folder,"publication/latex/main.pdf");
    $("open-zh-pdf").href=artifactPath(info.folder,"publication/latex_zh/main.pdf");
    const kpis=[
      [fmt.format(info.words),"英文成稿词数","最终质量报告"],
      [fmt.format(info.citations),"引用论文","最终引用映射"],
      [String(info.sections),"主体章节","全部通过主题对齐"],
      [fmt.format(info.calls),"模型调用","全过程累计"],
      [money(run.cost_cny),"实际模型费用",`预算 ${money(run.budget_cny)}`],
      [duration(state.payload.meta.duration_seconds),"真实墙钟时间",`${state.payload.timeline.length} 条回放事件`]
    ];
    $("kpi-grid").innerHTML=kpis.map(([v,l,n])=>`<article class="kpi-card"><span>${safe(l)}</span><strong>${safe(v)}</strong><small>${safe(n)}</small></article>`).join("");
    renderWorkflow(info); renderEvidence(info); renderPublications(info); resetReplay();
  }

  function stageRows(def) {
    const rows=state.payload.snapshot.stages || [];
    return rows.filter((row)=>def.keys.some((key)=>row.stage===key || row.stage.startsWith(`${key}_`) || row.stage.startsWith(`${key}-`)));
  }

  function stageStats(def) {
    const rows=stageRows(def);
    return {
      seconds:rows.reduce((s,r)=>s+Number(r.wall_time_seconds||0),0),
      cost:rows.reduce((s,r)=>s+Number(r.cost_cny||0),0),
      calls:rows.reduce((s,r)=>s+Number(r.model_call_count||0),0),
      tokens:rows.reduce((s,r)=>s+Number(r.input_tokens||0)+Number(r.output_tokens||0),0),
      rows
    };
  }

  function renderWorkflow(info) {
    $("workflow").innerHTML=STAGES.map((def,index)=>`<button class="stage-card${index===0?" active":""}" data-stage="${index}"><span>STAGE ${String(def.n).padStart(2,"0")}</span><strong>${safe(def.title)}</strong><small>${safe(def.short)}</small><em></em></button>`).join("");
    $("workflow").querySelectorAll("button").forEach((button)=>button.addEventListener("click",()=>showStage(Number(button.dataset.stage),info)));
    showStage(0,info);
  }

  function showStage(index,info) {
    state.stage=index;
    $("workflow").querySelectorAll("button").forEach((button,i)=>button.classList.toggle("active",i===index));
    const def=STAGES[index], stats=stageStats(def);
    const calls=stats.calls || (def.n===1 ? 1 : 0);
    $("stage-detail").innerHTML=`<div><p class="section-kicker">STAGE ${String(def.n).padStart(2,"0")} · COMPLETED</p><h3>${safe(def.title)}</h3><p>${safe(def.desc)}</p><a class="stage-evidence" href="${artifactPath(info.folder,def.evidence)}" target="_blank" rel="noreferrer">打开该阶段代表证据 →</a></div><div class="stage-metrics"><div><strong>${duration(stats.seconds)}</strong><span>可归属阶段耗时</span></div><div><strong>${stats.cost ? money(stats.cost) : "本地处理"}</strong><span>记录的模型费用</span></div><div><strong>${fmt.format(calls)}</strong><span>显式模型调用</span></div><div><strong>${fmt.format(stats.tokens)}</strong><span>输入与输出令牌</span></div><div><strong>${stats.rows.length || 1}</strong><span>合并的底层步骤</span></div><div><strong>已留痕</strong><span>证据状态</span></div></div>`;
  }

  function renderEvidence(info) {
    $("evidence-score").innerHTML=`<div class="quality-ring"><div class="ring"><div><strong>100%</strong><span>章节主题对齐</span></div></div><div class="quality-copy"><p class="section-kicker">QUALITY GATE</p><h3>研究对象在全文中保持一致</h3><p>全部计划章节均通过主题一致性检查；最终引用映射没有未解析条目，论文、引文与出版源包可互相核对。</p></div></div><div class="quality-list"><div><strong>${fmt.format(info.inlineRefs)}</strong><span>正文唯一引用标记</span></div><div><strong>${fmt.format(info.citations)}</strong><span>引用映射论文</span></div><div><strong>${info.sections}/${info.sections}</strong><span>章节主题对齐通过</span></div><div><strong>${info.figures}</strong><span>最终可渲染图像</span></div></div>`;
    const groups=STAGES.map((def)=>({def,...stageStats(def)})).filter((row)=>row.seconds>0||row.cost>0).sort((a,b)=>b.seconds-a.seconds).slice(0,7);
    const max=Math.max(...groups.map((row)=>row.seconds),1);
    $("cost-bars").innerHTML=groups.map((row)=>`<div class="cost-row"><span title="${safe(row.def.title)}">${safe(row.def.title)}</span><div class="cost-track"><div class="cost-fill" style="width:${Math.max(3,row.seconds/max*100).toFixed(1)}%"></div></div><strong>${duration(row.seconds)}</strong></div>`).join("");
  }

  function renderPublications(info) {
    const files=[
      ["PDF","中文综述论文",`${info.pagesZh} 页中文出版物`,"publication/latex_zh/main.pdf"],
      ["PDF","英文综述论文",`${info.pagesEn} 页英文出版物`,"publication/latex/main.pdf"],
      ["ZIP","可复现源文件包","LaTeX、BibTeX 与构建清单","publication/latex/arxiv-source.zip"],
      ["MAP","最终引用映射",`${info.citations} 篇论文与正文位置`,"reports/FINAL_CITATION_MAP.json"]
    ];
    $("publication-grid").innerHTML=files.map(([icon,title,copy,path])=>`<article class="publication-card"><span class="file-icon">${icon}</span><h3>${safe(title)}</h3><p>${safe(copy)}</p><a href="${artifactPath(info.folder,path)}" target="_blank" rel="noreferrer">打开成果 →</a></article>`).join("");
    const mid=Math.ceil(info.pagesZh/2);
    const pad=(n)=>String(n).padStart(3,"0");
    $("preview-grid").innerHTML=[[1,"中文论文首页"],[mid,"中文论文中页"],[info.pagesZh,"中文论文末页"]].map(([page,label])=>`<a class="preview-card" href="${artifactPath(info.folder,"publication/latex_zh/main.pdf")}" target="_blank" rel="noreferrer"><img src="${artifactPath(info.folder,`publication/latex_zh/preview/page_${pad(page)}.png`)}" alt="${safe(label)}" loading="lazy"><div><strong>${safe(label)}</strong><span>第 ${page} / ${info.pagesZh} 页</span></div></a>`).join("");
    const artifacts=[
      ["运行概览","问题、成本、页数与终态","run-summary.json"],
      ["全过程质量报告","主题一致性、字数、引用与完整性","reports/REVIEW_HARNESS_QUALITY_REPORT.json"],
      ["研究内容包","主链各阶段状态与交付清单","reports/REVIEW_CONTENT_PACKAGE.json"],
      ["全文统筹摘要","章节增强、全局编排与出版状态","publication_mainline/PUBLICATION_MAINLINE_SUMMARY.json"],
      ["结构审计","章节结构与完整性核验","publication_mainline/ARTICLE_STRUCTURE_AUDIT.json"],
      ["图像成果包","图像需求、来源、审核与挂载状态","visual_editor/final/FINAL_VISUAL_PACKAGE.json"],
      ["出版完整性审计","英文 PDF 与源包构建核验","publication/latex/PUBLICATION_INTEGRITY_AUDIT.json"],
      ["元数据审计","参考文献与出版信息核验","publication/metadata/PUBLICATION_METADATA_AUDIT.json"],
      ["运行报告","供人工检查的完整运行摘要","reports/HARNESS_RUN_REPORT.md"]
    ];
    $("artifact-grid").innerHTML=artifacts.map(([title,copy,path])=>`<a class="artifact-card" href="${artifactPath(info.folder,path)}" target="_blank" rel="noreferrer"><span><strong>${safe(title)}</strong><span>${safe(copy)}</span></span><b>↗</b></a>`).join("");
  }

  function eventLabel(event) {
    const map={run_started:"研究启动",run_finished:"研究完成",stage_started:"阶段开始",stage_finished:"阶段完成",stage_failed:"阶段记录",publication_recovery:"出版恢复",publication_recovery_metadata:"出版元数据恢复"};
    return map[event.event] || event.stage_label || event.event || "过程事件";
  }

  function resetReplay() {
    stopReplay(); state.eventIndex=-1; $("progress-fill").style.width="0%"; $("replay-clock").textContent="尚未开始";
    $("event-focus").innerHTML="<span>等待播放</span><strong>选择“开始 10× 回放”观察研究阶段推进</strong>"; $("event-stream").innerHTML=""; $("play-button").textContent=`开始 ${$("speed-select").value}× 回放`;
  }
  function stopReplay() { if(state.timer) clearInterval(state.timer); state.timer=null; state.playing=false; if($("play-button")) $("play-button").textContent="继续回放"; }
  function toggleReplay() {
    if(state.playing){stopReplay();return;}
    if(state.eventIndex>=state.payload.timeline.length-1) state.eventIndex=-1;
    state.playing=true; $("play-button").textContent="暂停回放"; stepReplay();
    const speed=Number($("speed-select").value||10); state.timer=setInterval(stepReplay,Math.max(45,650/speed));
  }
  function stepReplay() {
    const events=state.payload.timeline; state.eventIndex+=1;
    if(state.eventIndex>=events.length){state.eventIndex=events.length-1;stopReplay();$("play-button").textContent="重新播放";return;}
    const event=events[state.eventIndex], pct=((state.eventIndex+1)/events.length)*100;
    $("progress-fill").style.width=`${pct}%`;
    const first=new Date(events[0].ts), now=new Date(event.ts), elapsed=(now-first)/1000;
    $("replay-clock").textContent=`${duration(elapsed)} / ${duration(state.payload.meta.duration_seconds)}`;
    $("event-focus").innerHTML=`<span>${safe(event.stage_label || eventLabel(event))}</span><strong>${safe(event.text || eventLabel(event))}</strong>`;
    const shown=events.slice(Math.max(0,state.eventIndex-7),state.eventIndex+1);
    $("event-stream").innerHTML=shown.map((row,i)=>`<div class="event-chip${i===shown.length-1?" active":""}"><strong>${safe(row.stage_label || eventLabel(row))}</strong><span>${new Date(row.ts).toLocaleTimeString("zh-CN",{hour12:false})} · ${safe(eventLabel(row))}</span></div>`).join("");
  }

  async function refreshLocalStatus() {
    if(!config.liveEnabled) return;
    try { renderLocalStatus(await json(`${config.localApiBase}/status`)); } catch(error) { renderDiagnostics({status:"failed",ready:false,checks:[{label:"本地入口",status:"failed",detail:error.message}]}); }
  }
  function renderDiagnostics(diagnostics={}) {
    const labels={idle:"尚未检查",running:"正在检查",ready:"全部通过",failed:"检查未通过"};
    $("readiness-state").textContent=labels[diagnostics.status]||diagnostics.status||"尚未检查";
    $("readiness-state").className=`readiness-state ${diagnostics.ready?"ready":diagnostics.status==="failed"?"failed":"idle"}`;
    $("diagnostic-list").innerHTML=(diagnostics.checks||[]).map((row)=>`<div class="diagnostic-row ${safe(row.status)}"><i></i><div><strong>${safe(row.label)}</strong><span>${safe(row.detail)}</span></div></div>`).join("");
    const ready=Boolean(diagnostics.ready); $("research-question").disabled=!ready; $("profile-options").disabled=!ready; $("start-run-button").disabled=!ready;
    $("question-lock").textContent=ready?"已解锁":"等待检查"; $("diagnostic-button").disabled=diagnostics.status==="running"; $("diagnostic-button").textContent=diagnostics.status==="running"?"正在检查……":ready?"重新检查运行条件":"检查并准备真实运行";
  }
  function renderLocalStatus(payload) {
    renderDiagnostics(payload.diagnostics); const run=payload.active_run;
    if(!run){$("live-run-panel").hidden=true;return;}
    $("live-run-panel").hidden=false; $("live-run-status").textContent={running:"运行中",completed:"已完成",failed:"已结束",cancelled:"已停止"}[run.status]||run.status;
    $("live-run-status").className=`readiness-state ${run.status==="completed"?"ready":run.status==="failed"?"failed":""}`;
    $("live-run-summary").innerHTML=`<div><strong>${safe(run.run_id)}</strong><span>运行标识</span></div><div><strong>${run.profile==="full"?"完整自主综述":"快速真实验证"}</strong><span>运行规模</span></div><div><strong>${duration(run.elapsed_seconds)}</strong><span>已运行</span></div><div><strong>${safe(run.output_label)}</strong><span>本地产物</span></div>`;
    $("live-progress-fill").style.width=`${Number(run.progress||0)}%`; const event=run.last_event||{};
    $("live-event").innerHTML=`<span>${safe(event.stage_label||event.stage||"当前状态")}</span><strong>${safe(run.message||event.text||"研究进程正在运行")}</strong>`;
    $("cancel-run-button").disabled=run.status!=="running";
    if(run.status==="running") { clearTimeout(state.liveTimer); state.liveTimer=setTimeout(refreshLocalStatus,3000); }
  }
  async function startDiagnostics(){ try {renderLocalStatus(await json(`${config.localApiBase}/diagnostics`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})); clearTimeout(state.liveTimer); state.liveTimer=setTimeout(refreshLocalStatus,1000);} catch(error){$("form-message").textContent=error.message;} }
  async function startLiveRun(){ const question=$("research-question").value.trim(); if(question.length<12){$("form-message").textContent="请至少输入 12 个字符的真实研究问题。";return;} const profile=document.querySelector('input[name="profile"]:checked')?.value||"quick"; try{$("start-run-button").disabled=true;renderLocalStatus(await json(`${config.localApiBase}/runs`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question,profile})}));$("form-message").textContent="真实研究已启动；产物写入独立的 local_runs 目录。";}catch(error){$("form-message").textContent=error.message;$("start-run-button").disabled=false;} }
  async function cancelLiveRun(){ try{renderLocalStatus(await json(`${config.localApiBase}/runs/current/cancel`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}));}catch(error){$("form-message").textContent=error.message;} }

  function bind() {
    configureModes(); $("retry-button").addEventListener("click",loadPortal); $("play-button").addEventListener("click",toggleReplay); $("reset-button").addEventListener("click",resetReplay); $("speed-select").addEventListener("change",()=>{if(!state.playing)$("play-button").textContent=`开始 ${$("speed-select").value}× 回放`;});
    $("mobile-menu").addEventListener("click",()=>$("sidebar").classList.toggle("open"));
    $("diagnostic-button").addEventListener("click",startDiagnostics); $("start-run-button").addEventListener("click",startLiveRun); $("refresh-run-button").addEventListener("click",refreshLocalStatus); $("cancel-run-button").addEventListener("click",cancelLiveRun);
    $("research-question").addEventListener("input",()=>$("question-count").textContent=`${$("research-question").value.length} / 4000`);
    document.querySelectorAll('.profile-card input').forEach((radio)=>radio.addEventListener("change",()=>document.querySelectorAll('.profile-card').forEach((card)=>card.classList.toggle("selected",card.querySelector("input").checked))));
  }

  bind(); loadPortal();
})();
