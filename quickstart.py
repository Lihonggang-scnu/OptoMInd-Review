"""Stable local entry point for doctor, run, resume and replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quickstart.py",
        description="OptoMind Review local research pipeline",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="check the local environment without paying for a call")
    sub.add_parser("help", help="show this public command guide")
    descriptions = {
        "run": "启动一次真实研究任务；执行前会先经过意图确认与预检。",
        "resume": "从合法断点恢复已有研究任务。",
        "replay": "启动只读静态回放；只读取公开演示产物，不调用模型或文献服务。",
    }
    for command, help_text in (
        ("run", "run the research harness with the remaining arguments"),
        ("resume", "resume a research harness run with the remaining arguments"),
        ("replay", "open the read-only replay when replay assets are present"),
    ):
        command_parser = sub.add_parser(command, help=help_text, description=descriptions[command])
        command_parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _delegate_harness(args: list[str], *, resume: bool) -> int:
    import run_review_harness

    forwarded = list(args)
    if resume and "--run-dir" not in forwarded:
        raise SystemExit("resume requires --run-dir <existing run directory>")
    old_argv = sys.argv
    try:
        sys.argv = [str(PROJECT_ROOT / "run_review_harness.py"), *forwarded]
        return int(run_review_harness.main())
    finally:
        sys.argv = old_argv


def _run_replay(args: list[str]) -> int:
    try:
        from optomind_research.runtime.static_replay import run_server
    except ImportError:
        print(
            "Replay is not installed in this mainline-only upgrade checkout yet. "
            "Use `quickstart.py doctor` or run the research harness.",
            file=sys.stderr,
        )
        return 2
    return int(run_server(project_root=PROJECT_ROOT, argv=args))


def main(argv: list[str] | None = None) -> int:
    parsed = _parser().parse_args(argv)
    command = parsed.command or "help"
    if command in {"help", None}:
        _parser().print_help()
        return 0
    if command == "doctor":
        from optomind_research.runtime.doctor import build_doctor_report

        print(json.dumps(build_doctor_report(project_root=PROJECT_ROOT), ensure_ascii=False, indent=2))
        return 0
    if command == "run":
        return _delegate_harness(parsed.args, resume=False)
    if command == "resume":
        return _delegate_harness(parsed.args, resume=True)
    if command == "replay":
        return _run_replay(parsed.args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
