"""Local Web UI for testing Query Planner with human-in-the-loop confirmation."""

from __future__ import annotations

import json
import re
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HANDOFF_ROOT = PROJECT_ROOT / "outputs" / "query_planner_handoffs"


def create_app(*, real_llm: bool = True, model_tier: str = "premium_model", prompt_path: str | None = None):
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse

    from config.qwen_config import get_model_name, validate_qwen_config
    from optomind_research.query_planner import DEFAULT_QUERY_PLANNER_PROMPT_PATH, QueryPlannerAgent

    app = FastAPI(title="OptoMind Query Planner", version="0.2-human-review")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def make_agent() -> QueryPlannerAgent:
        return QueryPlannerAgent(
            prompt_path=prompt_path,
            model_tier=model_tier,
            repair_model_tier="standard_model",
            real_llm=real_llm,
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    @app.get("/api/version")
    def version() -> dict[str, Any]:
        cfg = validate_qwen_config()
        return {
            "agent": "QueryPlannerAgent",
            "role": "User question -> problem understanding + scope definition + keyword decomposition",
            "real_llm": real_llm,
            "primary_model_tier": model_tier,
            "primary_model_name": get_model_name(model_tier),
            "repair_model_tier": "standard_model",
            "repair_model_name": get_model_name("standard_model"),
            "prompt_path": str(prompt_path or DEFAULT_QUERY_PLANNER_PROMPT_PATH),
            "has_api_key": cfg.get("has_api_key", False),
            "api_key_source": cfg.get("api_key_source", ""),
            "human_in_loop": True,
        }

    @app.post("/api/query-planner")
    def query_planner(body: dict[str, Any] = Body()) -> dict[str, Any]:
        question = str(body.get("question", "") or body.get("user_query", "") or "").strip()
        if not question:
            raise HTTPException(400, "question required")
        try:
            return make_agent().plan_review_dict(question)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=20),
                },
            ) from exc

    @app.post("/api/query-planner/validate")
    def validate_query_plan(body: dict[str, Any] = Body()) -> dict[str, Any]:
        payload = body.get("payload")
        question = str(body.get("question", "") or "").strip()
        if payload is None:
            raise HTTPException(400, "payload required")
        try:
            return make_agent().validate_user_payload(payload, user_query=question)
        except Exception as exc:
            raise HTTPException(500, {"error": str(exc), "traceback": traceback.format_exc(limit=20)}) from exc

    @app.post("/api/query-planner/confirm")
    def confirm_query_plan(body: dict[str, Any] = Body()) -> dict[str, Any]:
        payload = body.get("payload")
        question = str(body.get("question", "") or "").strip()
        human_note = str(body.get("human_note", "") or "").strip()
        if payload is None:
            raise HTTPException(400, "payload required")

        agent = make_agent()
        checked = agent.validate_user_payload(payload, user_query=question)
        if not checked.get("ok"):
            raise HTTPException(400, checked)

        normalized = checked["normalized"]
        if question:
            normalized["input"]["user_query"] = question
        handoff_dir = _new_handoff_dir(question or normalized["input"].get("user_query", "query"))
        handoff_dir.mkdir(parents=True, exist_ok=True)
        handoff = {
            "stage": "query_planner_confirmed",
            "status": "ready_for_next_stage",
            "next_stage": "retrieval_planning",
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "human_note": human_note,
            "plan": normalized,
            "validation": checked["validation"],
        }
        handoff_path = handoff_dir / "query_planner_confirmed.json"
        handoff_path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ok": True,
            "status": "ready_for_next_stage",
            "next_stage": "retrieval_planning",
            "handoff_dir": str(handoff_dir),
            "handoff_path": str(handoff_path),
            "handoff": handoff,
        }

    return app


def _new_handoff_dir(question: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", question).strip("-")[:40]
    safe = safe or "query"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return HANDOFF_ROOT / f"{stamp}-{safe}-{uuid.uuid4().hex[:6]}"


def start_web(port: int = 8860, *, real_llm: bool = True, model_tier: str = "premium_model", prompt_path: str | None = None) -> None:
    import uvicorn

    app = create_app(real_llm=real_llm, model_tier=model_tier, prompt_path=prompt_path)
    print(f"\n  OptoMind Query Planner -> http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


_INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>OptoMind Query Planner</title>
  <style>
    :root{--bg:#0d1117;--surface:#161b22;--surface2:#0f1620;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--purple:#d8b4fe}
    body{max-width:1180px;margin:24px auto;padding:0 16px;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    h1{font-size:23px;color:var(--accent);margin:0 0 6px}.sub{color:var(--muted);margin-bottom:14px}
    textarea,input{box-sizing:border-box;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:12px;font:inherit}
    textarea{width:100%;resize:vertical}#q{min-height:92px}#edit{min-height:360px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}
    button{margin:10px 8px 10px 0;padding:10px 18px;border:0;border-radius:8px;background:#238636;color:white;font-weight:700;cursor:pointer}
    button.secondary{background:#1f6feb}button.warn{background:#9e6a03}button:disabled{opacity:.55;cursor:not-allowed}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.card{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;min-height:110px;white-space:pre-wrap}
    .card h2{margin:0 0 8px;font-size:15px;color:var(--accent)}.wide{grid-column:1/-1}
    #status{min-height:26px;color:var(--green);margin:8px 0}.err{color:var(--red)}.ok{color:var(--green)}.warntext{color:var(--yellow)}.muted{color:var(--muted)}
    .pill{display:inline-block;border:1px solid var(--border);border-radius:999px;padding:3px 9px;margin:2px 5px 2px 0;background:var(--surface);font-size:12px}.pill.ok{border-color:var(--green)}.pill.bad{border-color:var(--red);color:var(--red)}.pill.warn{border-color:var(--yellow);color:var(--yellow)}
    .three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}.small{font-size:12px;color:var(--muted)}.path{color:var(--purple);word-break:break-all}
    @media(max-width:900px){.grid,.three{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <h1>Query Planner</h1>
  <div class="sub">Two-layer guardrail: qwen3.7-max generates the JSON; Python validates schema and English-only generated fields; qwen3.6-flash repairs JSON format only if needed; a human can edit and confirm the handoff file.</div>
  <textarea id="q" placeholder="Enter a vague research question. Example: What are the research opportunities at the intersection of daytime radiative cooling and agricultural films?"></textarea>
  <br>
  <button id="runBtn" onclick="runPlanner()">1. Run Query Planner</button>
  <button class="secondary" id="validateBtn" onclick="validateEdited()" disabled>2. Validate edited JSON</button>
  <button class="warn" id="confirmBtn" onclick="confirmEdited()" disabled>3. Confirm and create handoff file</button>
  <div id="status"></div>

  <div class="three">
    <div class="card"><h2>Format Pipeline</h2><div id="pipeline"></div></div>
    <div class="card"><h2>Program Validation</h2><div id="validation"></div></div>
    <div class="card"><h2>Handoff Status</h2><div id="handoff"></div></div>
  </div>

  <div class="grid">
    <div class="card"><h2>1. Problem Understanding</h2><div id="understanding"></div></div>
    <div class="card"><h2>2. Scope Definition</h2><div id="scope"></div></div>
    <div class="card wide"><h2>3. Keyword Decomposition</h2><div id="keywords"></div></div>
    <div class="card wide">
      <h2>Human-editable JSON</h2>
      <div class="small">If the model's understanding, scope, or keywords are inaccurate, edit the JSON below and then validate it.</div>
      <textarea id="edit"></textarea>
    </div>
  </div>

<script>
let lastQuestion='';
let lastNormalized=null;

function esc(s){return String(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function pill(text, cls){return '<span class="pill '+(cls||'')+'">'+esc(text)+'</span>';}
function setStatus(text, cls){document.getElementById('status').innerHTML='<span class="'+(cls||'')+'">'+esc(text)+'</span>';}
function parseEdit(){
  const text=document.getElementById('edit').value.trim();
  if(!text) throw new Error('The editor is empty.');
  return text;
}
function renderValidation(v){
  if(!v){document.getElementById('validation').textContent='None yet';return;}
  const parts=[pill(v.ok?'passed':'failed', v.ok?'ok':'bad')];
  if(v.errors && v.errors.length) parts.push('\nErrors:\n'+v.errors.map(x=>'· '+x).join('\n'));
  if(v.warnings && v.warnings.length) parts.push('\nWarnings:\n'+v.warnings.map(x=>'· '+x).join('\n'));
  document.getElementById('validation').innerHTML=parts.join('');
}
function renderResult(payload){
  const out=(payload||{}).output||{};
  const scope=out.scope_definition||{};
  const kw=out.keyword_decomposition||{};
  document.getElementById('understanding').textContent=out.problem_understanding||'';
  document.getElementById('scope').textContent=(scope.main_scope||'')+'\n\n'+(scope.scope_items||[]).map((x,i)=>`${i+1}. ${x}`).join('\n');
  document.getElementById('keywords').textContent=(kw.keywords||[]).join('、');
}
function renderPipeline(data){
  const primaryOk=data.primary_validation && data.primary_validation.ok;
  const repair=data.repair||{};
  const finalOk=data.final_validation && data.final_validation.ok;
  let html='';
  html+=pill('primary model: '+(primaryOk?'valid':'invalid'), primaryOk?'ok':'bad');
  html+=pill('repair model: '+(repair.attempted?'triggered':'not triggered'), repair.attempted?'warn':'ok');
  html+=pill('final JSON: '+(finalOk?'valid':'invalid'), finalOk?'ok':'bad');
  html+='\n\nStatus: '+esc(data.status||'');
  html+='\nNote: '+esc(data.note||'');
  document.getElementById('pipeline').innerHTML=html;
}
async function runPlanner(){
  const q=document.getElementById('q').value.trim();
  if(!q) return;
  lastQuestion=q; lastNormalized=null;
  document.getElementById('runBtn').disabled=true;
  document.getElementById('validateBtn').disabled=true;
  document.getElementById('confirmBtn').disabled=true;
  document.getElementById('handoff').textContent='';
  setStatus('Running: qwen3.7-max generation -> program validation -> qwen3.6-flash format-only repair if needed...', '');
  try{
    const resp=await fetch('/api/query-planner',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});
    const data=await resp.json();
    if(!resp.ok) throw new Error(JSON.stringify(data));
    lastNormalized=data.result;
    document.getElementById('edit').value=JSON.stringify(data.result,null,2);
    renderPipeline(data);
    renderValidation(data.final_validation);
    renderResult(data.result);
    document.getElementById('validateBtn').disabled=false;
    document.getElementById('confirmBtn').disabled=!(data.final_validation&&data.final_validation.ok);
    setStatus('Completed: review the content; edit JSON and revalidate if needed.', 'ok');
  }catch(e){
    setStatus('Failed: '+e.message, 'err');
  }finally{
    document.getElementById('runBtn').disabled=false;
  }
}
async function validateEdited(){
  try{
    setStatus('Validating edited JSON...', '');
    const payload=parseEdit();
    const resp=await fetch('/api/query-planner/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:lastQuestion,payload})});
    const data=await resp.json();
    if(!resp.ok) throw new Error(JSON.stringify(data));
    renderValidation(data.validation);
    if(data.ok){
      lastNormalized=data.normalized;
      document.getElementById('edit').value=JSON.stringify(data.normalized,null,2);
      renderResult(data.normalized);
      document.getElementById('confirmBtn').disabled=false;
      setStatus('Validation passed: you can now confirm and create the next-stage handoff file.', 'ok');
    }else{
      document.getElementById('confirmBtn').disabled=true;
      setStatus('Validation failed: keep editing according to the errors.', 'err');
    }
  }catch(e){
    document.getElementById('confirmBtn').disabled=true;
    setStatus('Validation failed: '+e.message, 'err');
  }
}
async function confirmEdited(){
  try{
    setStatus('Confirming and saving handoff file...', '');
    const payload=parseEdit();
    const resp=await fetch('/api/query-planner/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:lastQuestion,payload})});
    const data=await resp.json();
    if(!resp.ok) throw new Error(JSON.stringify(data));
    document.getElementById('handoff').innerHTML=pill('confirmed','ok')+pill(data.next_stage||'retrieval_planning','ok')+'\n\nHandoff file:\n<span class="path">'+esc(data.handoff_path||'')+'</span>';
    setStatus('Confirmed: this Query Planner output is ready for the next retrieval-planning stage.', 'ok');
  }catch(e){
    setStatus('Confirmation failed: '+e.message, 'err');
  }
}
</script>
</body>
</html>"""
