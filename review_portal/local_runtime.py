"""Standard-library local server for replay and verified Review execution."""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_error(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return f"{type(exc).__name__}: {text[:260]}" if text else type(exc).__name__


def _last_json_line(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    last: dict[str, Any] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, Mapping):
                    last = {
                        key: row[key]
                        for key in ("event", "stage", "stage_label", "text", "status", "ts")
                        if key in row
                    }
    except OSError:
        return {}
    return last


def _progress_for(stage: str, event: str, status: str) -> int:
    if status in {"completed", "failed", "cancelled"}:
        return 100
    if event == "run_finished":
        return 100
    ordered = (
        ("query_planner", 7),
        ("s2_literature_intelligence", 16),
        ("topic_scoped_kb", 24),
        ("section_coverage", 34),
        ("phase3_argument_orchestration", 46),
        ("authoring_revision", 57),
        ("publication_mainline_enhancement", 66),
        ("publication_mainline_commander", 74),
        ("publication_mainline_staged_completion", 82),
        ("visual", 88),
        ("publication_metadata", 92),
        ("chinese_translation", 96),
        ("latex_publication_zh", 99),
    )
    value = 3
    for prefix, progress in ordered:
        if stage.startswith(prefix):
            value = progress
    return value


class LocalRuntimeController:
    def __init__(
        self,
        *,
        project_root: Path,
        key_dir: Path,
        local_run_root: Path,
        runtime_preparer: Callable[[], Path],
        quick_command_builder: Callable[..., list[str]],
        full_command_builder: Callable[..., list[str]],
    ) -> None:
        self.project_root = project_root.resolve()
        self.key_dir = key_dir.resolve()
        self.local_run_root = local_run_root.resolve()
        self.runtime_preparer = runtime_preparer
        self.quick_command_builder = quick_command_builder
        self.full_command_builder = full_command_builder
        self._lock = threading.RLock()
        self._runtime_python: Path | None = None
        self._credentials: tuple[Path, Path] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any = None
        self._started: float | None = None
        self.diagnostics: dict[str, Any] = {
            "status": "idle", "ready": False, "checks": [],
            "started_at_utc": None, "finished_at_utc": None,
        }
        self.active_run: dict[str, Any] | None = None

    def _check(self, check_id: str, label: str, status: str, detail: str) -> None:
        with self._lock:
            rows = list(self.diagnostics.get("checks") or [])
            payload = {"id": check_id, "label": label, "status": status, "detail": detail}
            for index, row in enumerate(rows):
                if row.get("id") == check_id:
                    rows[index] = payload
                    break
            else:
                rows.append(payload)
            self.diagnostics["checks"] = rows

    @staticmethod
    def _credential(path: Path, *, required: bool) -> Path:
        if path.is_file() and path.stat().st_size > 0:
            return path.resolve()
        if required:
            raise RuntimeError(f"缺少非空密钥文件：api_keys/{path.name}")
        return path.resolve()

    def start_diagnostics(self) -> None:
        with self._lock:
            if self.diagnostics.get("status") == "running":
                return
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("真实研究正在运行，不能同时重新检查环境。")
            labels = (
                ("assets", "项目与回放资产", "正在核对主链、三组回放和最终成果。"),
                ("credentials", "本地服务密钥", "等待检查。"),
                ("runtime", "Python 科研环境", "等待检查。"),
                ("qwen", "Qwen 模型服务", "等待最小真实请求。"),
                ("literature", "学术文献服务", "等待最小真实检索。"),
            )
            self.diagnostics = {
                "status": "running", "ready": False, "started_at_utc": _utc_now(),
                "finished_at_utc": None,
                "checks": [
                    {"id": check_id, "label": label, "status": "running" if index == 0 else "pending", "detail": detail}
                    for index, (check_id, label, detail) in enumerate(labels)
                ],
            }
        threading.Thread(target=self._run_diagnostics, daemon=True).start()

    def _run_diagnostics(self) -> None:
        try:
            required = (
                self.project_root / "run_review_harness.py",
                self.project_root / "requirements-research.txt",
                self.project_root / "replay" / "replay-manifest.json",
                self.project_root / "artifacts" / "e2e",
            )
            missing = [path.name for path in required if not path.exists()]
            if missing:
                raise RuntimeError("缺少项目资产：" + "、".join(missing))
            manifest = json.loads((self.project_root / "replay" / "replay-manifest.json").read_text(encoding="utf-8"))
            if len(manifest.get("runs") or []) != 3:
                raise RuntimeError("正式回放清单不是预期的三组运行。")
            self._check("assets", "项目与回放资产", "passed", "Review 主链、三组回放与最终成果包均已找到。")

            self._check("credentials", "本地服务密钥", "running", "正在核对密钥文件是否存在。")
            qwen = self._credential(self.key_dir / "qwen-api-key.txt", required=True)
            s2 = self._credential(self.key_dir / "semantic-scholar-api-key.txt", required=False)
            self._credentials = (qwen, s2)
            self._check("credentials", "本地服务密钥", "passed", "Qwen 密钥已就绪；文献服务可使用密钥池或公共访问。")

            self._check("runtime", "Python 科研环境", "running", "正在检查或安装隔离依赖。")
            self._runtime_python = Path(self.runtime_preparer()).resolve()
            self._check("runtime", "Python 科研环境", "passed", f"科研依赖已就绪（{self._runtime_python.name}）。")

            env = os.environ.copy()
            env.update({
                "PYTHONUTF8": "1",
                "QWEN_API_KEY_FILE": str(qwen),
                "DASHSCOPE_API_KEY_FILE": str(qwen),
                "SEMANTIC_SCHOLAR_API_KEYS_FILE": str(s2),
            })
            self._check("qwen", "Qwen 模型服务", "running", "正在发起最小真实模型请求。")
            self._check("literature", "学术文献服务", "running", "正在发起最小真实文献检索。")
            completed = subprocess.run(
                [str(self._runtime_python), "-u", str(self.project_root / "scripts" / "probe_review_connectivity.py")],
                cwd=self.project_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=150, check=False,
            )
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            payload = json.loads(lines[-1]) if lines else {}
            for check_id, label in (("qwen", "Qwen 模型服务"), ("literature", "学术文献服务")):
                row = next((item for item in payload.get("checks") or [] if item.get("id") == check_id), {})
                self._check(check_id, label, str(row.get("status") or "failed"), str(row.get("detail") or "连通测试没有返回有效状态。"))
            with self._lock:
                ready = completed.returncode == 0 and all(row.get("status") == "passed" for row in self.diagnostics.get("checks") or [])
                self.diagnostics.update({"ready": ready, "status": "ready" if ready else "failed", "finished_at_utc": _utc_now()})
        except BaseException as exc:
            with self._lock:
                pending = [row for row in self.diagnostics.get("checks") or [] if row.get("status") in {"running", "pending"}]
            target = pending[0] if pending else {"id": "runtime", "label": "运行准备"}
            self._check(str(target["id"]), str(target["label"]), "failed", _safe_error(exc))
            with self._lock:
                for row in list(self.diagnostics.get("checks") or []):
                    if row.get("status") in {"running", "pending"}:
                        self._check(str(row["id"]), str(row["label"]), "failed", "前置检查未通过，因此未执行此项。")
                self.diagnostics.update({"ready": False, "status": "failed", "finished_at_utc": _utc_now()})

    def start_run(self, *, question: str, profile: str) -> None:
        normalized = " ".join(question.split())
        if len(normalized) < 12 or len(normalized) > 4000:
            raise ValueError("研究问题必须包含 12 至 4000 个字符。")
        if profile not in {"quick", "full"}:
            raise ValueError("运行规模必须是 quick 或 full。")
        with self._lock:
            if not self.diagnostics.get("ready") or self._runtime_python is None or self._credentials is None:
                raise RuntimeError("运行准备检查尚未全部通过。")
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("已有真实研究正在运行；本地入口一次只执行一个任务。")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            run_id = f"review-{profile}-{stamp}"
            run_dir = self.local_run_root / run_id
            question_dir = self.local_run_root / "_questions"
            log_dir = self.local_run_root / "_portal_logs"
            question_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)
            question_file = question_dir / f"{run_id}.txt"
            question_file.write_text(question.strip() + "\n", encoding="utf-8")
            qwen, s2 = self._credentials
            builder = self.quick_command_builder if profile == "quick" else self.full_command_builder
            command = builder(self._runtime_python, question_file=question_file, run_dir=run_dir, qwen_file=qwen)
            env = os.environ.copy()
            env.update({
                "PYTHONUTF8": "1", "QWEN_API_KEY_FILE": str(qwen),
                "DASHSCOPE_API_KEY_FILE": str(qwen), "SEMANTIC_SCHOLAR_API_KEYS_FILE": str(s2),
            })
            log_path = log_dir / f"{run_id}.log"
            self._log_handle = log_path.open("wb")
            self._process = subprocess.Popen(command, cwd=self.project_root, env=env, stdout=self._log_handle, stderr=subprocess.STDOUT)
            self._started = time.monotonic()
            self.active_run = {
                "run_id": run_id, "profile": profile, "question": question.strip(), "status": "running",
                "started_at_utc": _utc_now(), "finished_at_utc": None, "elapsed_seconds": 0.0,
                "output_label": f"local_runs/{run_id}", "output_dir": str(run_dir), "progress": 2,
                "last_event": {}, "message": "研究进程已启动，等待第一条阶段事件。", "exit_code": None,
            }
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        with self._lock:
            process = self._process
        if process is None:
            return
        code = process.wait()
        with self._lock:
            if self._log_handle is not None:
                self._log_handle.close(); self._log_handle = None
            if self.active_run is None:
                return
            run_dir = Path(str(self.active_run["output_dir"]))
            has_result = (run_dir / "REVIEW_CONTENT_PACKAGE.json").is_file()
            if self.active_run.get("status") != "cancelled":
                self.active_run["status"] = "completed" if code == 0 and has_result else "failed"
            self.active_run.update({
                "exit_code": code, "finished_at_utc": _utc_now(), "progress": 100,
                "message": "研究成果已经写入本地运行目录。" if code == 0 and has_result else "研究进程已经结束；日志与已生成产物均被保留。",
            })

    def cancel_run(self) -> None:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                raise RuntimeError("当前没有正在运行的研究任务。")
            self._process.terminate()
            if self.active_run is not None:
                self.active_run.update({"status": "cancelled", "finished_at_utc": _utc_now(), "message": "停止信号已发送；已有产物会被保留。"})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self.active_run is not None:
                if self._started is not None and self.active_run.get("status") == "running":
                    self.active_run["elapsed_seconds"] = round(time.monotonic() - self._started, 1)
                run_dir = Path(str(self.active_run["output_dir"]))
                event = _last_json_line(run_dir / "HARNESS_EVENTS.jsonl")
                if event:
                    self.active_run["last_event"] = event
                    self.active_run["progress"] = max(int(self.active_run.get("progress") or 0), _progress_for(str(event.get("stage") or ""), str(event.get("event") or ""), str(self.active_run.get("status") or "")))
                    self.active_run["message"] = str(event.get("text") or self.active_run.get("message") or "")
            active = None if self.active_run is None else {key:value for key,value in self.active_run.items() if key != "output_dir"}
            return {
                "schema_version": "optomind.review.local-portal.v1", "mode": "review", "live_enabled": True,
                "diagnostics": json.loads(json.dumps(self.diagnostics)), "active_run": json.loads(json.dumps(active)),
            }


class LocalPortalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, project_root: Path, ui_root: Path, controller: LocalRuntimeController) -> None:
        super().__init__(address, LocalPortalHandler)
        self.project_root = project_root.resolve()
        self.ui_root = ui_root.resolve()
        self.controller = controller


class LocalPortalHandler(BaseHTTPRequestHandler):
    server: LocalPortalServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, payload: Mapping[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    def _payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的请求长度。") from exc
        if length < 0 or length > 16 * 1024:
            raise ValueError("请求内容不能超过 16 KiB。")
        if not length:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求内容必须是 JSON 对象。")
        return value

    def _static(self, url_path: str) -> None:
        if url_path == "/assets/config.js":
            body = (
                "window.OPTOMIND_PORTAL_CONFIG=Object.freeze({productMode:'review',deployment:'local',liveEnabled:true,"
                "manifestPath:'replay-manifest.json',artifactBase:'artifacts/e2e',localApiBase:'/api/local',"
                "repositoryUrl:'https://github.com/Lihonggang-scnu/OptoMInd-Review'});\n"
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "application/javascript; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body); return
        if url_path.startswith("/artifacts/"):
            base = (self.server.project_root / "artifacts").resolve()
            relative = url_path.removeprefix("/artifacts/")
        else:
            base = self.server.ui_root
            relative = url_path.lstrip("/") or "index.html"
        candidate = (base / relative).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "路径不在公开目录中"); return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            self._error(HTTPStatus.NOT_FOUND, "文件不存在"); return
        body = candidate.read_bytes(); content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store" if candidate.suffix in {".json", ".js"} else "public, max-age=300"); self.end_headers(); self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path == "/api/local/status":
            self._json(self.server.controller.snapshot()); return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            payload = self._payload()
            if path == "/api/local/diagnostics":
                self.server.controller.start_diagnostics()
            elif path == "/api/local/runs":
                self.server.controller.start_run(question=str(payload.get("question") or ""), profile=str(payload.get("profile") or "quick"))
            elif path == "/api/local/runs/current/cancel":
                self.server.controller.cancel_run()
            else:
                self._error(HTTPStatus.NOT_FOUND, "本地接口不存在"); return
        except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc)); return
        self._json(self.server.controller.snapshot(), HTTPStatus.ACCEPTED)


def serve_local_portal(*, project_root: Path, ui_root: Path, controller: LocalRuntimeController, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("真实提问入口只能监听本机回环地址。")
    try:
        server = LocalPortalServer((host, port), project_root=project_root, ui_root=ui_root, controller=controller)
    except OSError:
        if port == 0:
            raise
        server = LocalPortalServer((host, 0), project_root=project_root, ui_root=ui_root, controller=controller)
        print(f"端口 {port} 不可用，已自动选择本机可用端口。")
    url = f"http://{host}:{server.server_port}/"
    print(f"OptoMind Review 统一前端已启动：{url}")
    print("三组静态回放立即可用；真实提问将在连通检查全部通过后激活。")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
