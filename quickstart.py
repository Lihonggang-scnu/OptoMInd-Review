"""One public entry point for OptoMind Review replay and live research."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPLAY_ROOT = ROOT / "replay"
KEY_DIR = ROOT / "api_keys"
LOCAL_RUN_ROOT = ROOT / "local_runs"
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements-research.txt"
HARNESS = ROOT / "run_review_harness.py"

RUNTIME_IMPORTS = (
    "agentscope", "fitz", "ftfy", "json_repair", "lxml", "matplotlib",
    "numpy", "openai", "pandas", "pydantic", "sklearn", "yaml",
)


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def _venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _runtime_ready(python: str | Path) -> bool:
    probe = "; ".join(f"import {name}" for name in RUNTIME_IMPORTS)
    completed = subprocess.run(
        [str(python), "-c", probe], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return completed.returncode == 0


def prepare_runtime(*, use_current_env: bool = False) -> Path:
    current = Path(sys.executable).resolve()
    if _runtime_ready(current):
        print(f"科研运行环境已就绪：{current}")
        return current
    if use_current_env:
        raise RuntimeError("当前 Python 缺少科研依赖。请去掉 --use-current-env，让入口自动创建 .venv。")
    python = _venv_python()
    if not python.is_file():
        print("首次真实运行：正在创建项目隔离环境 .venv……")
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(VENV_DIR)
    if not _runtime_ready(python):
        print("首次真实运行：正在安装 Review 科研依赖，耗时取决于网络与本机环境……")
        code = subprocess.call(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
            cwd=ROOT,
        )
        if code != 0 or not _runtime_ready(python):
            raise RuntimeError("依赖安装未完成，请检查网络后再次运行同一个入口。")
    print(f"科研运行环境已就绪：{python}")
    return python.resolve()


def _base_command(python: str | Path, *, question_file: Path, run_dir: Path, qwen_file: Path) -> list[str]:
    return [
        str(python), "-u", str(HARNESS),
        "--question-file", str(question_file.resolve()),
        "--run-dir", str(run_dir.resolve()),
        "--output-root", str(LOCAL_RUN_ROOT.resolve()),
        "--qwen-key-file", str(qwen_file.resolve()),
        "--execution-profile", "private_study",
        "--auto-confirm-query-plan",
        "--no-research-plan",
    ]


def quick_command(python: str | Path, *, question_file: Path, run_dir: Path, qwen_file: Path) -> list[str]:
    """Real evidence-to-draft chain with publication-heavy steps bounded."""
    return _base_command(python, question_file=question_file, run_dir=run_dir, qwen_file=qwen_file) + [
        "--global-budget-cny", "3.0",
        "--oa-fulltext-paper-cap", "2",
        "--visual-max-generated-images", "0",
        "--no-real-visual-audit",
        "--no-real-image-generation",
        "--no-latex-publication",
        "--no-chinese-publication",
        "--no-publication-mainline-representative-applications",
        "--no-llm-style-pipeline-enabled",
        "--no-chapter-style-governance-enabled",
    ]


def full_command(python: str | Path, *, question_file: Path, run_dir: Path, qwen_file: Path) -> list[str]:
    """The same publication mainline used by the three formal E2E records."""
    return _base_command(python, question_file=question_file, run_dir=run_dir, qwen_file=qwen_file) + [
        "--global-budget-cny", "15.0",
        "--visual-review-auto-accept-seconds", "30",
    ]


def run_ui(args: argparse.Namespace) -> int:
    from review_portal.local_runtime import LocalRuntimeController, serve_local_portal

    controller = LocalRuntimeController(
        project_root=ROOT,
        key_dir=Path(args.key_dir).expanduser().resolve(),
        local_run_root=LOCAL_RUN_ROOT,
        runtime_preparer=lambda: prepare_runtime(use_current_env=bool(args.use_current_env)),
        quick_command_builder=quick_command,
        full_command_builder=full_command,
    )
    serve_local_portal(
        project_root=ROOT, ui_root=REPLAY_ROOT, controller=controller,
        port=int(args.port), open_browser=not bool(args.no_open),
    )
    return 0


def run_doctor(args: argparse.Namespace) -> int:
    manifest = REPLAY_ROOT / "replay-manifest.json"
    if not manifest.is_file():
        print("静态回放：缺少 replay-manifest.json")
        return 2
    import json
    runs = json.loads(manifest.read_text(encoding="utf-8")).get("runs") or []
    print(f"静态回放：{len(runs)}/3 组正式运行可见。")
    qwen = Path(args.key_dir).expanduser().resolve() / "qwen-api-key.txt"
    print("真实提问密钥：" + ("Qwen 文件已放置（内容未输出）。" if qwen.is_file() and qwen.stat().st_size else "尚未放置非空 qwen-api-key.txt。"))
    print("当前 Python 依赖：" + ("已就绪。" if _runtime_ready(sys.executable) else "未就绪；点击前端检查后会自动安装。"))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="OptoMind Review 评审统一入口")
    sub = root.add_subparsers(dest="command")
    ui = sub.add_parser("ui", aliases=["portal"], help="打开静态回放与真实提问统一前端")
    ui.add_argument("--key-dir", default=str(KEY_DIR), help="本地 api_keys 文件夹")
    ui.add_argument("--port", type=int, default=8765, help="本机端口；0 表示自动选择")
    ui.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ui.add_argument("--use-current-env", action="store_true", help="不自动创建隔离环境")
    doctor = sub.add_parser("doctor", help="检查回放、密钥文件和运行依赖")
    doctor.add_argument("--key-dir", default=str(KEY_DIR), help="本地 api_keys 文件夹")
    return root


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = parser().parse_args(argv)
    if args.command is None:
        args = parser().parse_args(["ui"])
    try:
        return run_doctor(args) if args.command == "doctor" else run_ui(args)
    except KeyboardInterrupt:
        print("\n已停止。")
        return 130
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"\n无法继续：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
