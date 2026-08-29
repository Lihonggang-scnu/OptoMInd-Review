"""OptoMind preflight doctor (F5 change 4).

Reports environment readiness for a local run. Checks are READ-ONLY:
API keys are verified via file existence + non-empty size only -- the
implementation never reads, prints, or logs their contents.

Entry points:
    python -m optomind_ui.preflight          (human-readable, exit code)
    GET /api/preflight                       (JSON for the F6 panel)

Nothing is ever auto-installed; each missing item carries a copy-pasteable
fix hint instead.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DISK_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB headroom

@dataclass
class CheckResult:
    key: str
    label: str
    status: str            # ok | missing | degraded
    detail: str
    fix_hint: str = ""
    blocking: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "fix_hint": self.fix_hint,
            "blocking": self.blocking,
        }


def _check_python() -> CheckResult:
    version = sys.version_info
    ok = version >= (3, 11)
    return CheckResult(
        key="python",
        label="Python 版本",
        status="ok" if ok else "missing",
        detail=" ".join(sys.version.split())[:80],
        fix_hint="安装 Python 3.11+（推荐 3.12）后重试",
        blocking=True,
    )


_REQUIRED_MODULES = ("fastapi", "uvicorn", "numpy", "pymupdf", "sse_starlette", "agentscope")


def _check_pip_modules() -> List[CheckResult]:
    results: List[CheckResult] = []
    for name in _REQUIRED_MODULES:
        try:
            importlib.import_module(name)
            results.append(CheckResult(
                key="pip:" + name,
                label="依赖包 " + name,
                status="ok",
                detail="可导入",
                blocking=True,
            ))
        except Exception as exc:
            results.append(CheckResult(
                key="pip:" + name,
                label="依赖包 " + name,
                status="missing",
                detail="导入失败：" + type(exc).__name__,
                fix_hint="uv pip install -r requirements-research.txt",
                blocking=True,
            ))
    return results


def _check_api_key() -> CheckResult:
    """Existence + non-empty size ONLY. Contents are never read or logged."""

    key_file = PROJECT_ROOT / "api_keys" / "qwen-api-key.txt"
    if key_file.is_file():
        try:
            non_empty = key_file.stat().st_size > 0
        except OSError:
            non_empty = False
        if non_empty:
            return CheckResult(
                key="api_key",
                label="DashScope API key（api_keys/qwen-api-key.txt）",
                status="ok",
                detail="文件存在且非空（内容未读取）",
                blocking=True,
            )
        return CheckResult(
            key="api_key",
            label="DashScope API key（api_keys/qwen-api-key.txt）",
            status="missing",
            detail="文件存在但为空",
            fix_hint="将我们私发的 DashScope key 放入 api_keys/qwen-api-key.txt",
            blocking=True,
        )
    return CheckResult(
        key="api_key",
        label="DashScope API key（api_keys/qwen-api-key.txt）",
        status="missing",
        detail="未找到 api_keys/qwen-api-key.txt",
        fix_hint="将我们私发的 DashScope key 放入 api_keys/qwen-api-key.txt",
        blocking=True,
    )


def _find_binary(name: str) -> str:
    return shutil.which(name) or ""


def _check_latex() -> List[CheckResult]:
    latexmk = _find_binary("latexmk")
    xelatex = _find_binary("xelatex")
    if latexmk and xelatex:
        return [CheckResult(
            key="latex",
            label="LaTeX 工具链（latexmk/xelatex）",
            status="ok",
            detail="PDF 编译可用",
        )]
    missing = [name for name, found in (("latexmk", latexmk), ("xelatex", xelatex)) if not found]
    return [CheckResult(
        key="latex",
        label="LaTeX 工具链（latexmk/xelatex）",
        status="degraded",
        detail=("缺 " + ", ".join(missing) + "；将跳过 PDF 编译，仍生成 .tex/.md"),
        fix_hint="安装 TeX Live 或 MiKTeX（可选；不装只影响 PDF）",
        blocking=False,
    )]


_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)


def _check_cjk_fonts() -> CheckResult:
    present = [name for name in _CJK_FONT_CANDIDATES if Path(name).is_file()]
    if present:
        return CheckResult(
            key="cjk_fonts",
            label="TeX 中文字体",
            status="ok",
            detail="检测到系统中文字体：" + Path(present[0]).name,
        )
    return CheckResult(
        key="cjk_fonts",
        label="TeX 中文字体",
        status="degraded",
        detail="未检测到常见中文字体；中文 PDF 可能出现字体异常",
        fix_hint="安装 SimSun / 微软雅黑等中文字体后重试（非必需）",
        blocking=False,
    )


def _check_disk_space() -> CheckResult:
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
        free_gb = usage.free / (1024 ** 3)
    except OSError as exc:
        return CheckResult(
            key="disk", label="磁盘可用空间", status="degraded",
            detail="无法探测：" + type(exc).__name__, blocking=False,
        )
    if free_gb >= 5:
        return CheckResult(
            key="disk",
            label="磁盘可用空间",
            status="ok",
            detail="剩余约 " + str(round(free_gb, 1)) + " GB",
        )
    return CheckResult(
        key="disk", label="磁盘可用空间", status="degraded",
        detail="仅剩 " + str(round(free_gb, 1)) + " GB（建议 ≥ 5 GB）",
        fix_hint="清理项目所在盘空间",
        blocking=False,
    )


def _check_playwright_kernel() -> CheckResult:
    # NOTE: absence is OK on purpose -- the institutional_access branch that
    # needs Chromium is disabled by default; reporting it as `missing` would
    # mislead users into a pointless ~400 MB download.
    try:
        import playwright  # noqa: F401

        installed = True
    except Exception:
        installed = False
    if installed:
        return CheckResult(
            key="playwright",
            label="Playwright 浏览器内核（可选分支）",
            status="ok",
            detail="已安装（institutional 分支可用）",
        )
    return CheckResult(
        key="playwright",
        label="Playwright 浏览器内核（可选分支）",
        status="ok",
        detail="默认 OA 路线不需要；未安装属正常状态",
        fix_hint="仅当需要机构访问分支时：pip install -e \".[institutional]\" && playwright install chromium",
        blocking=False,
    )


def check_all() -> List[CheckResult]:
    results: List[CheckResult] = [
        _check_python(),
        *_check_pip_modules(),
        _check_api_key(),
    ]
    results.extend(_check_latex())
    results.append(_check_cjk_fonts())
    results.append(_check_disk_space())
    results.append(_check_playwright_kernel())
    return results

def _blocking_failures(results: List["CheckResult"]) -> List["CheckResult"]:
    return [item for item in results if item.blocking and item.status != "ok"]


def _main() -> int:
    results = check_all()
    width = max(len(item.label) for item in results)
    for item in results:
        mark = {"ok": "[OK]", "missing": "[缺失]", "degraded": "[降级]"}[item.status]
        line = " ".join((mark, item.label.ljust(width), "-", str(item.detail)))
        print(line)
        if item.status != "ok" and item.fix_hint:
            print("      修复建议：", item.fix_hint)
    failures = _blocking_failures(results)
    if failures:
        print()
        print(f"存在 {len(failures)} 项阻塞性缺失，运行无法开始。")
        return 1
    print()
    print("全部阻塞项就绪。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
