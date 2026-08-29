"""Local Web UI for Literature Resource Builder stage."""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_JOB_ROOT = PROJECT_ROOT / "outputs" / "literature_resource_builder" / "web_jobs"
DEFAULT_SCNU_LIBVPN_LOGIN_URL = "https://libvpn.scnu.edu.cn/portal/?redirect_uri=https%3A%2F%2Flib-scnu-edu-cn-s.libvpn.scnu.edu.cn%3A20080%2F#!/login"


def create_app(*, real_llm: bool = True):
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse

    from optomind_research.config import ResearchSettings
    from optomind_research.literature_resource_builder import DEFAULT_INSTITUTION_PROFILE_DIR, LiteratureResourceBuilder, load_query_plan
    from optomind_research.query_planner import QueryPlannerAgent

    app = FastAPI(title="OptoMind Literature Resource Builder", version="1.0-stage2")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = threading.RLock()

    def now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def append_log(job: dict[str, Any], phase: str, doing: str, result: str = "", reason: str = "", **payload: Any) -> None:
        event = {
            "index": len(job["logs"]) + 1,
            "time": now(),
            "phase": phase,
            "doing": doing,
            "result": result,
            "reason": reason,
            "payload": payload,
        }
        with jobs_lock:
            job["logs"].append(event)
        log_path = Path(job["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def normalize_options(body: dict[str, Any]) -> dict[str, Any]:
        backends = body.get("backends")
        if isinstance(backends, str):
            backends = [x.strip() for x in backends.split(",") if x.strip()]
        if not isinstance(backends, list) or not backends:
            backends = ["openalex", "crossref", "semantic_scholar_public", "arxiv", "brave", "tavily", "serper"]
        academic_results = int(body.get("academic_results_per_backend") or 50)
        web_results = int(body.get("web_results_per_backend") or 10)
        _academic = {"openalex", "crossref", "semantic_scholar_public", "arxiv", "core"}
        _web = {"brave", "tavily", "serper"}
        per_backend_results = {}
        for b in backends:
            if b in _academic:
                per_backend_results[b] = academic_results
            elif b in _web:
                per_backend_results[b] = web_results
        return {
            "backends": [str(x).strip() for x in backends if str(x).strip()],
            "max_queries": int(body.get("max_queries") or 10),
            "results_per_backend": academic_results,
            "per_backend_results": per_backend_results,
            "from_year": int(body.get("from_year") or 2014),
            "max_abstract_candidates": int(body.get("max_abstract_candidates") or 500),
            "feature_candidate_limit": int(body.get("feature_candidate_limit") or 60),
            "scoring_batch_size": int(body.get("scoring_batch_size") or 12),
            "scoring_max_workers": int(body.get("scoring_max_workers") or 4),
            "fulltext_top_n": int(body.get("fulltext_top_n") or 150),
            "feature_top_k": int(body.get("feature_top_k") or 10),
            "max_fulltext_downloads": int(body.get("max_fulltext_downloads") or 150),
            "max_features": int(body.get("max_features") or 10),
            "source_audit_limit": int(body.get("source_audit_limit") or 300),
            "enable_web_lens_supplement": bool(body.get("enable_web_lens_supplement", True)),
            "web_lens_content_limit": int(body.get("web_lens_content_limit") or 300),
            "web_lens_extraction_batch_size": int(body.get("web_lens_extraction_batch_size") or 8),
            "enable_query_expansion": bool(body.get("enable_query_expansion", True)),
            "enable_citation_expansion": bool(body.get("enable_citation_expansion", False)),
            "max_reference_dois": int(body.get("max_reference_dois") or 3),
            "enable_institutional_access": bool(body.get("enable_institutional_access", False)),
            "institution_profile_dir": str(body.get("institution_profile_dir") or DEFAULT_INSTITUTION_PROFILE_DIR),
            "institution_browser_channel": str(body.get("institution_browser_channel") or "edge-cdp"),
            "institution_cdp_endpoint": str(body.get("institution_cdp_endpoint") or "http://127.0.0.1:9222"),
        }

    def load_query_plan_from_body(body: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        pasted = str(body.get("query_plan_json") or "").strip()
        path = str(body.get("query_plan_path") or "").strip().strip('"')
        question = str(body.get("question") or "").strip()
        if pasted:
            append_log(job, "input", "Read pasted Query Planner JSON")
            return json.loads(pasted)
        if path:
            append_log(job, "input", "Read Query Planner JSON file", result=path)
            return load_query_plan(path)
        if question:
            append_log(job, "query_planner", "Run Query Planner first to generate a retrieval plan", reason="The page received only a natural-language question.")
            plan = QueryPlannerAgent(real_llm=real_llm).plan_dict(question)
            append_log(job, "query_planner", "Query Planner completed", result="Problem understanding, scope definition, and keyword decomposition are ready.")
            return plan
        raise ValueError("Provide at least one of question, query_plan_json, or query_plan_path.")

    def run_job(job_id: str, body: dict[str, Any]) -> None:
        job = jobs[job_id]
        try:
            job["status"] = "running"
            options = normalize_options(body)
            with jobs_lock:
                job["options"] = options
            append_log(job, "start", "Background job started", result=f"job_id={job_id}", options=options)
            query_plan = load_query_plan_from_body(body, job)
            artifact_dir = WEB_JOB_ROOT / job_id / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            builder = LiteratureResourceBuilder(
                real_llm=real_llm,
                backends=options["backends"],
                institution_profile_dir=options["institution_profile_dir"],
                institution_browser_channel=options["institution_browser_channel"],
                institution_cdp_endpoint=options["institution_cdp_endpoint"],
                enable_institutional_access=options["enable_institutional_access"],
            )

            def progress(event: dict[str, Any]) -> None:
                append_log(
                    job,
                    str(event.get("phase") or "builder"),
                    str(event.get("doing") or ""),
                    str(event.get("result") or ""),
                    str(event.get("reason") or ""),
                    **(event.get("payload") or {}),
                )

            result = builder.run(
                query_plan,
                max_queries=options["max_queries"],
                results_per_backend=options["results_per_backend"],
                per_backend_results=options["per_backend_results"],
                from_year=options["from_year"],
                max_abstract_candidates=options["max_abstract_candidates"],
                feature_candidate_limit=options["feature_candidate_limit"],
                scoring_batch_size=options["scoring_batch_size"],
                scoring_max_workers=options["scoring_max_workers"],
                fulltext_top_n=options["fulltext_top_n"],
                feature_top_k=options["feature_top_k"],
                max_fulltext_downloads=options["max_fulltext_downloads"],
                max_features=options["max_features"],
                source_audit_limit=options["source_audit_limit"],
                enable_web_lens_supplement=options["enable_web_lens_supplement"],
                web_lens_content_limit=options["web_lens_content_limit"],
                web_lens_extraction_batch_size=options["web_lens_extraction_batch_size"],
                enable_query_expansion=options["enable_query_expansion"],
                enable_citation_expansion=options["enable_citation_expansion"],
                max_reference_dois=options["max_reference_dois"],
                progress_callback=progress,
                artifact_dir=artifact_dir,
            )
            next_bundle = result.get("resource_bundle_for_next_agent") or {}
            session = result.get("resource_update_session") or {}
            facet_map = session.get("facet_literature_map") or {}
            facet_recall = session.get("facet_bibliometric_recall") or {}
            web_lens_context = session.get("web_lens_context") or {}
            supplemental_facet_plan = session.get("supplemental_facet_plan") or {}
            supplemental_features = ((supplemental_facet_plan.get("supplemental_facet_plan") or {}).get("supplemental_features") or [])
            origin_counts: dict[str, int] = {}
            for item in next_bundle.get("available_fulltexts", []):
                origin = str(item.get("origin") or "unknown")
                origin_counts[origin] = origin_counts.get(origin, 0) + 1
            summary = {
                "artifact_dir": result.get("artifact_dir"),
                "resource_bundle_path": result.get("resource_bundle_path"),
                "options": options,
                "research_facets": len(facet_map.get("facets", []) or []),
                "web_lens_raw_results": len(web_lens_context.get("raw_web_results", []) or []),
                "web_context_summaries": len(web_lens_context.get("web_context_summaries", []) or []),
                "supplemental_facets": len(supplemental_features),
                "facet_bibliometric_new_records": ((facet_recall.get("totals") or {}).get("new_records", 0)),
                "facet_bibliometric_external_calls": ((facet_recall.get("totals") or {}).get("external_calls", 0)),
                "abstract_records_considered": len((session.get("screening_result") or {}).get("candidate_papers_from_abstracts", [])),
                "selected_for_fulltext": len((session.get("screening_result") or {}).get("selected_for_fulltext_upgrade", [])),
                "available_fulltexts": len(next_bundle.get("available_fulltexts", [])),
                "available_fulltext_origin_counts": origin_counts,
                "local_cached_fulltexts": len(next_bundle.get("local_cached_fulltexts", [])),
                "quality_gate_summary": next_bundle.get("quality_gate_summary", {}),
                "downstream_core_fulltexts": len(next_bundle.get("downstream_core_fulltexts", [])),
                "background_pages_cached": len(next_bundle.get("background_pages_cached", [])),
                "abstract_only_high_value_papers": len(next_bundle.get("abstract_only_high_value_papers", [])),
                "manual_download_list": len(next_bundle.get("manual_download_list", [])),
                "manual_download_folder": next_bundle.get("manual_download_folder", ""),
                "manual_download_table": next_bundle.get("manual_download_list", [])[:200],
                "library_stats": session.get("library_stats", {}),
            }
            with jobs_lock:
                job["status"] = "completed"
                job["result"] = result
                job["summary"] = summary
                job["artifact_dir"] = result.get("artifact_dir", "")
            append_log(job, "done", "Background job completed", result=json.dumps(summary, ensure_ascii=False))
        except Exception as exc:
            with jobs_lock:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["traceback"] = traceback.format_exc(limit=30)
            append_log(job, "error", "Background job failed", result=type(exc).__name__, reason=str(exc), traceback=job["traceback"])

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    _server_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @app.get("/api/version")
    def version() -> dict[str, Any]:
        settings = ResearchSettings()
        src = Path(__file__)
        return {
            "stage": "Literature Resource Builder",
            "role": "Query Planner JSON -> structured literature element library / source audit / scholarly facets / feature scoring / full-text resource package",
            "real_llm": real_llm,
            "capabilities": settings.capability_status(),
            "web_job_root": str(WEB_JOB_ROOT),
            "server_started_at": _server_started_at,
            "code_updated_at": datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }

    @app.get("/api/literature-resource-builder/fulltexts")
    def list_fulltexts() -> dict[str, Any]:
        from optomind_research.literature_resource_builder import LiteratureResourceLibrary, DEFAULT_LIBRARY_DB
        lib = LiteratureResourceLibrary(DEFAULT_LIBRARY_DB)
        rows = lib.conn.execute(
            "SELECT paper_id, title, doi, access_method, downloaded_at, parsed_text_path, local_file_path"
            " FROM fulltext_records WHERE fulltext_status='available' ORDER BY downloaded_at DESC"
        ).fetchall()
        items = [dict(r) for r in rows]
        return {"count": len(items), "fulltexts": items}

    @app.get("/api/literature-resource-builder/institution/status")
    def institution_status() -> dict[str, Any]:
        from tools.academic_backends.institutional_access_backend import InstitutionalAccessBackend

        backend = InstitutionalAccessBackend(profile_dir=DEFAULT_INSTITUTION_PROFILE_DIR, enabled=True, headless=True, browser_channel="edge-cdp")
        return backend.check_status()

    @app.post("/api/literature-resource-builder/institution/login")
    def institution_login(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from tools.academic_backends.institutional_access_backend import InstitutionalAccessBackend

        start_url = str(body.get("start_url") or DEFAULT_SCNU_LIBVPN_LOGIN_URL)
        profile_dir = str(body.get("profile_dir") or DEFAULT_INSTITUTION_PROFILE_DIR)
        browser_channel = str(body.get("browser_channel") or "edge-cdp")
        cdp_endpoint = str(body.get("cdp_endpoint") or "http://127.0.0.1:9222")
        result_holder: dict[str, Any] = {"status": "started", "profile_dir": profile_dir, "start_url": start_url, "browser_channel": browser_channel, "cdp_endpoint": cdp_endpoint}

        def _login() -> None:
            backend = InstitutionalAccessBackend(profile_dir=profile_dir, enabled=True, headless=False, browser_channel=browser_channel, cdp_endpoint=cdp_endpoint)
            try:
                result_holder.update(backend.manual_login_session(start_url=start_url, wait_for_enter=False, timeout_seconds=900))
            except Exception as exc:
                result_holder.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        thread = threading.Thread(target=_login, daemon=True)
        thread.start()
        return {"ok": True, "message": "Institution login window should open. Complete or refresh login there, then keep the Edge window open for publisher HTML fetching.", **result_holder}

    @app.post("/api/literature-resource-builder/start")
    def start(body: dict[str, Any] = Body()) -> dict[str, Any]:
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        job_dir = WEB_JOB_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": now(),
            "logs": [],
            "summary": {},
            "options": {},
            "result": None,
            "error": "",
            "traceback": "",
            "artifact_dir": "",
            "log_path": str(job_dir / "job_logs.jsonl"),
        }
        with jobs_lock:
            jobs[job_id] = job
        thread = threading.Thread(target=run_job, args=(job_id, body), daemon=True)
        thread.start()
        return {"ok": True, "job_id": job_id, "status_url": f"/api/literature-resource-builder/jobs/{job_id}"}

    @app.get("/api/literature-resource-builder/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                raise HTTPException(404, "job not found")
            return {
                "job_id": job_id,
                "status": job.get("status"),
                "created_at": job.get("created_at"),
                "logs": job.get("logs", [])[-500:],
                "summary": job.get("summary", {}),
                "options": job.get("options", {}),
                "artifact_dir": job.get("artifact_dir", ""),
                "log_path": job.get("log_path", ""),
                "error": job.get("error", ""),
                "traceback": job.get("traceback", ""),
            }

    return app


def start_web(port: int = 8861, *, real_llm: bool = True) -> None:
    import uvicorn

    app = create_app(real_llm=real_llm)
    print(f"\n  OptoMind Literature Resource Builder -> http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OptoMind Literature Resource Builder</title>
  <style>
    :root{--bg:#0d1117;--panel:#161b22;--panel2:#0f1620;--border:#30363d;--text:#d0d7de;--muted:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--purple:#d8b4fe}
    body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    main{max-width:1280px;margin:22px auto;padding:0 16px}
    h1{font-size:24px;margin:0;color:var(--accent)} .sub{color:var(--muted);margin:6px 0 16px}
    textarea,input{box-sizing:border-box;width:100%;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:10px;font:inherit}
    textarea{resize:vertical}.question{min-height:76px}.jsonbox{min-height:140px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
    button{padding:10px 18px;border:0;border-radius:8px;background:#238636;color:white;font-weight:700;cursor:pointer;margin:8px 8px 8px 0}button.secondary{background:#1f6feb}button:disabled{opacity:.55;cursor:not-allowed}
    .grid{display:grid;grid-template-columns:1.05fr .95fr;gap:12px}.card{background:var(--panel2);border:1px solid var(--border);border-radius:12px;padding:14px}.card h2{font-size:15px;margin:0 0 8px;color:var(--accent)}
    .row{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.small{font-size:12px;color:var(--muted)}.status{min-height:24px;color:var(--green)}
    .log{height:500px;overflow:auto;background:#05080d;border:1px solid var(--border);border-radius:10px;padding:10px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap}
    .logline{border-bottom:1px solid #161b22;padding:5px 0}.phase{color:var(--purple)}.doing{color:var(--accent)}.result{color:var(--green)}.reason{color:var(--yellow)}.err{color:var(--red)}.path{word-break:break-all;color:var(--purple)}
    .summary{white-space:pre-wrap;background:#05080d;border:1px solid var(--border);border-radius:10px;padding:10px;min-height:140px}
    table.manualTable{width:100%;border-collapse:collapse;font-size:12px;background:#05080d;border:1px solid var(--border)}
    table.manualTable th,table.manualTable td{border:1px solid var(--border);padding:6px;vertical-align:top}
    table.manualTable th{color:var(--accent);text-align:left;background:#0b1220}
    .wrap{word-break:break-all}
    @media(max-width:980px){.grid,.row{grid-template-columns:1fr}}
  </style>
</head>
<body>
<main>
  <h1>Literature Resource Builder</h1>
  <div class="sub">Input a natural-language question, Query Planner JSON, or a JSON file path. The system updates the structured literature element library, audits source credibility, plans scholarly facets, scores papers by feature, acquires full text, and produces a manual-download table. Key progress logs are printed in real time.</div>

  <div class="grid">
    <section class="card">
      <h2>Input</h2>
      <label>Natural-language question (optional; if filled, Query Planner runs first)</label>
      <textarea id="question" class="question" placeholder="Example: Review the research background, key spectral mechanisms, and application bottlenecks of passive daytime radiative cooling films."></textarea>
      <label>Or: Query Planner JSON file path</label>
      <input id="queryPlanPath" placeholder="outputs/query_planner_handoffs/<run>/query_planner_confirmed.json">
      <label>Or: paste Query Planner JSON</label>
      <textarea id="queryPlanJson" class="jsonbox" placeholder="{ ... }"></textarea>

      <h2>Run Parameters</h2>
      <label>Backends (comma-separated; CORE is not enabled by default to avoid ineffective consumption)</label>
      <input id="backends" value="openalex,crossref,semantic_scholar_public,arxiv,brave,tavily,serper">
      <div class="row">
        <input id="maxQueries" value="10" title="max_queries">
        <input id="academicResults" value="50" title="Academic backend results per query (OA/SS/arXiv/crossref)">
        <input id="webResults" value="10" title="Web backend results per query (brave/tavily/serper)">
        <input id="maxFeatures" value="10" title="max_features">
      </div>
      <div class="row">
        <input id="maxFulltexts" value="150" title="max_fulltext_downloads">
        <input id="scoringWorkers" value="4" title="scoring_max_workers">
        <input id="featureCandidateLimit" value="60" title="feature_candidate_limit: candidates per feature before scoring">
        <input id="fulltextTopN" value="150" title="fulltext_top_n: top-N papers by overall score for full-text candidacy">
        <input id="featureTopK" value="10" title="feature_top_k: per-feature top-K supplemental selection">
        <input id="maxCandidates" value="500" title="max_abstract_candidates">
        <input id="auditLimit" value="300" title="source_audit_limit">
      </div>
      <div class="small">Row 1: max_queries / academic backend results per query / web backend results per query / max_features. Row 2: max_fulltext_downloads / scoring_workers / feature_candidate_limit / fulltext_top_n / feature_top_k / max_abstract_candidates / audit_limit.</div>
      <button id="startBtn" onclick="startJob()">Start building literature resource library</button>
      <button class="secondary" onclick="loadVersion()">View capability status</button>
      <div style="margin-top:10px;border-top:1px solid var(--border);padding-top:10px">
        <h2>Institution session</h2>
        <label><input id="enableInstitution" type="checkbox" checked style="width:auto;margin-right:6px">Enable institutional Playwright session during fulltext fetching</label>
        <input id="institutionLoginUrl" value="https://libvpn.scnu.edu.cn/portal/?redirect_uri=https%3A%2F%2Flib-scnu-edu-cn-s.libvpn.scnu.edu.cn%3A20080%2F#!/login" title="Start URL for SCNU libvpn login">
        <input id="institutionBrowserChannel" value="edge-cdp" title="Browser channel: edge-cdp reuses the real logged-in Edge on port 9222; msedge launches a separate Playwright Edge">
        <input id="institutionCdpEndpoint" value="http://127.0.0.1:9222" title="CDP endpoint for real Edge attach mode">
        <button class="secondary" onclick="institutionStatus()">Check institution session</button>
        <button class="secondary" onclick="institutionLogin()">Open login browser</button>
        <div id="institutionInfo" class="small"></div>
      </div>
      <div id="status" class="status"></div>
      <div id="version" class="small"></div>
    </section>

    <section class="card">
      <h2>Result Summary</h2>
      <div id="summary" class="summary">Not run yet.</div>
      <h3>Full-text failures / manual download table</h3>
      <div class="small">When automatic legal full-text acquisition fails, download through the recommended routes and place PDF/HTML/XML files into the recommended directory. Stage 3 will scan and ingest them.</div>
      <div id="manualDownloads"></div>
    </section>
  </div>

  <section class="card" style="margin-top:12px">
    <h2>Real-time Progress Log</h2>
    <div class="small">This panel continuously shows what the system is doing, what result it got, and why. Full logs are also written to job_logs.jsonl.</div>
    <div id="log" class="log"></div>
  </section>

  <section class="card" style="margin-top:12px">
    <h2>Raw Full-text Cache (all historical cache)</h2>
    <button class="secondary" onclick="loadFulltexts()">Refresh downloaded full-text list</button>
    <div id="fulltextsInfo" class="small" style="margin:6px 0"></div>
    <div id="fulltextsList" class="summary" style="min-height:80px;font-size:12px;font-family:ui-monospace,Consolas,monospace">Click refresh to view cached full texts.</div>
  </section>
</main>

<script>
let currentJob=null;
let pollTimer=null;
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function setStatus(s, cls=''){document.getElementById('status').innerHTML='<span class="'+cls+'">'+esc(s)+'</span>';}
function renderLogs(logs){
  const box=document.getElementById('log');
  box.innerHTML=(logs||[]).map(e=>{
    const payload=e.payload&&Object.keys(e.payload).length?'\n  payload='+esc(JSON.stringify(e.payload,null,0)):'';
    return `<div class="logline">#${e.index||''} ${esc(e.time||'')} <span class="phase">[${esc(e.phase||'')}]</span> <span class="doing">${esc(e.doing||'')}</span>`+
      (e.result?` → <span class="result">${esc(e.result)}</span>`:'')+
      (e.reason?`; <span class="reason">reason: ${esc(e.reason)}</span>`:'')+payload+'</div>';
  }).join('');
  box.scrollTop=box.scrollHeight;
}
function renderSummary(data){
  const s=data.summary||{};
  let text='';
  text+='Status: '+(data.status||'')+'\n';
  text+='artifact_dir：'+(s.artifact_dir||data.artifact_dir||'')+'\n';
  text+='resource_bundle_path：'+(s.resource_bundle_path||'')+'\n\n';
  text+='Scholar Facets: '+(s.research_facets??0)+'\n';
  text+='Raw web signals: '+(s.web_lens_raw_results??0)+'\n';
  text+='Dense web summaries: '+(s.web_context_summaries??0)+'\n';
  text+='Supplemental facets: '+(s.supplemental_facets??0)+'\n';
  text+='New bibliometric supplemental records: '+(s.facet_bibliometric_new_records??0)+'\n';
  text+='Bibliometric external calls: '+(s.facet_bibliometric_external_calls??0)+'\n';
  text+='Candidate abstracts: '+(s.abstract_records_considered??0)+'\n';
  text+='Selected for full-text upgrade: '+(s.selected_for_fulltext??0)+'\n';
  text+='Available full texts: '+(s.available_fulltexts??0)+'\n';
  text+='Full-text origin distribution: '+JSON.stringify(s.available_fulltext_origin_counts||{})+'\n';
  text+='Relevant local cache additions: '+(s.local_cached_fulltexts??0)+'\n';
  text+='Quality gate: '+JSON.stringify(s.quality_gate_summary||{})+'\n';
  text+='Core full texts: '+(s.downstream_core_fulltexts??0)+'\n';
  text+='Background-page cache: '+(s.background_pages_cached??0)+'\n';
  text+='High-value abstract-only papers: '+(s.abstract_only_high_value_papers??0)+'\n';
  text+='Manual download list: '+(s.manual_download_list??0)+'\n\n';
  text+='Manual full-text drop folder: '+(s.manual_download_folder||'')+'\n';
  text+='Run parameters: '+JSON.stringify(s.options||data.options||{})+'\n\n';
  text+='Backend log: '+(data.log_path||'')+'\n';
  if(data.error) text+='\nError: '+data.error+'\n'+(data.traceback||'');
  document.getElementById('summary').textContent=text;
  renderManualDownloads(s.manual_download_table||[]);
}
function renderManualDownloads(items){
  const box=document.getElementById('manualDownloads');
  if(!items || !items.length){
    box.innerHTML='<div class="small">No manual full-text download is required.</div>';
    return;
  }
  const rows=items.map((x,i)=>{
    const routes=(x.recommended_download_routes||[]).map(r=>`<div>${esc(r)}</div>`).join('');
    return `<tr>
      <td>${i+1}</td>
      <td class="wrap">${esc(x.title||'')}</td>
      <td class="wrap">${esc(x.doi||'')}</td>
      <td>${esc(x.failure_category||'')}</td>
      <td class="wrap">${esc(x.reason_needed||'')}</td>
      <td class="wrap">${routes}</td>
      <td class="wrap">${esc(x.recommended_local_path||x.recommended_download_folder||'')}</td>
    </tr>`;
  }).join('');
  box.innerHTML=`<table class="manualTable">
    <thead><tr><th>#</th><th>Paper</th><th>DOI</th><th>Failure category</th><th>Failure reason</th><th>Recommended routes</th><th>Save to</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}
async function loadVersion(){
  const r=await fetch('/api/version'); const d=await r.json();
  document.getElementById('version').textContent=
    `Code updated: ${d.code_updated_at||'?'} | Server started: ${d.server_started_at||'?'} | LLM: ${d.real_llm?'real':'mock'}`;
}
async function loadFulltexts(){
  document.getElementById('fulltextsInfo').textContent='Loading...';
  const r=await fetch('/api/literature-resource-builder/fulltexts');
  const d=await r.json();
  document.getElementById('fulltextsInfo').textContent=`${d.count} cached full texts`;
  document.getElementById('fulltextsList').textContent=(d.fulltexts||[]).map((f,i)=>
    `${i+1}. [${f.downloaded_at||'?'}] ${f.title||f.paper_id} (${f.doi||'no DOI'}) [${f.access_method||'?'}]\n   parsed: ${f.parsed_text_path||'none'}`
  ).join('\n\n')||'No cached full text.';
}
async function institutionStatus(){
  const box=document.getElementById('institutionInfo');
  box.textContent='Checking institution session...';
  const r=await fetch('/api/literature-resource-builder/institution/status');
  const d=await r.json();
  box.textContent=JSON.stringify(d,null,2);
}
async function institutionLogin(){
  const box=document.getElementById('institutionInfo');
  const start_url=document.getElementById('institutionLoginUrl').value.trim()||'https://libvpn.scnu.edu.cn/portal/?redirect_uri=https%3A%2F%2Flib-scnu-edu-cn-s.libvpn.scnu.edu.cn%3A20080%2F#!/login';
  const browser_channel=document.getElementById('institutionBrowserChannel').value.trim()||'edge-cdp';
  const cdp_endpoint=document.getElementById('institutionCdpEndpoint').value.trim()||'http://127.0.0.1:9222';
  box.textContent='Opening login browser...';
  const r=await fetch('/api/literature-resource-builder/institution/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_url,browser_channel,cdp_endpoint})});
  const d=await r.json();
  box.textContent=JSON.stringify(d,null,2);
}
async function startJob(){
  const body={
    question:document.getElementById('question').value.trim(),
    query_plan_path:document.getElementById('queryPlanPath').value.trim(),
    query_plan_json:document.getElementById('queryPlanJson').value.trim(),
    backends:document.getElementById('backends').value.trim(),
    max_queries:Number(document.getElementById('maxQueries').value||10),
    academic_results_per_backend:Number(document.getElementById('academicResults').value||50),
    web_results_per_backend:Number(document.getElementById('webResults').value||10),
    max_features:Number(document.getElementById('maxFeatures').value||10),
    max_fulltext_downloads:Number(document.getElementById('maxFulltexts').value||150),
    scoring_max_workers:Number(document.getElementById('scoringWorkers').value||4),
    feature_candidate_limit:Number(document.getElementById('featureCandidateLimit').value||60),
    fulltext_top_n:Number(document.getElementById('fulltextTopN').value||150),
    feature_top_k:Number(document.getElementById('featureTopK').value||10),
    max_abstract_candidates:Number(document.getElementById('maxCandidates').value||500),
    source_audit_limit:Number(document.getElementById('auditLimit').value||300),
    enable_institutional_access:document.getElementById('enableInstitution').checked,
    institution_browser_channel:document.getElementById('institutionBrowserChannel').value.trim()||'edge-cdp',
    institution_cdp_endpoint:document.getElementById('institutionCdpEndpoint').value.trim()||'http://127.0.0.1:9222'
  };
  if(!body.question && !body.query_plan_path && !body.query_plan_json){setStatus('Provide at least one question, JSON file path, or pasted JSON.','err');return;}
  if(body.enable_institutional_access && String(body.institution_browser_channel||'').toLowerCase().includes('cdp')){
    setStatus('Checking Edge-CDP institution browser session...', '');
    const statusResp=await fetch('/api/literature-resource-builder/institution/status');
    const statusData=await statusResp.json();
    if(!statusData.cdp_available){
      setStatus('Edge-CDP is unavailable. Start real Edge with --remote-debugging-port=9222 and log into the institution/library/publisher session first.','err');
      document.getElementById('institutionInfo').textContent=JSON.stringify(statusData,null,2);
      return;
    }
    document.getElementById('institutionInfo').textContent=JSON.stringify(statusData,null,2);
  }
  document.getElementById('startBtn').disabled=true;
  setStatus('Background job submitted; starting...', '');
  document.getElementById('log').innerHTML='';
  const r=await fetch('/api/literature-resource-builder/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await r.json();
  if(!r.ok){setStatus('Start failed: '+JSON.stringify(data),'err');document.getElementById('startBtn').disabled=false;return;}
  currentJob=data.job_id;
  setStatus('Job started: '+currentJob);
  if(pollTimer) clearInterval(pollTimer);
  pollTimer=setInterval(pollJob,1200);
  pollJob();
}
async function pollJob(){
  if(!currentJob) return;
  const r=await fetch('/api/literature-resource-builder/jobs/'+currentJob);
  const data=await r.json();
  renderLogs(data.logs||[]);
  renderSummary(data);
  if(data.status==='completed'){setStatus('Completed: artifacts were written to artifact_dir.','');clearInterval(pollTimer);document.getElementById('startBtn').disabled=false;}
  if(data.status==='failed'){setStatus('Failed: check logs and traceback.','err');clearInterval(pollTimer);document.getElementById('startBtn').disabled=false;}
}
loadVersion();
</script>
</body>
</html>"""
