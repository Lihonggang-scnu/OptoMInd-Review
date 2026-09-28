"""Legal-only adapter for the vendored ScanSci PDF project.

This adapter intentionally disables grey/unauthorized sources. It is meant to
be used as a backup for lawful OA / publisher / institution-authorized PDF
retrieval, not as a replacement for OptoMind's structured XML/HTML resolver.
"""

from __future__ import annotations

import os
import sys
import multiprocessing as mp
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCANSCI_SRC = PROJECT_ROOT / "external_tools" / "scansci-pdf" / "src"
API_KEYS_DIR = PROJECT_ROOT / "api_keys"
DESKTOP = Path.home() / "Desktop"


def _first_nonempty_line(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            value = line.strip()
            if value:
                return value
    except Exception:
        return ""
    return ""


def _first_from_files(*paths: Path) -> str:
    for path in paths:
        value = _first_nonempty_line(path)
        if value:
            return value
    return ""


def _default_config() -> dict[str, Any]:
    email = (
        os.environ.get("UNPAYWALL_EMAIL")
        or os.environ.get("CONTACT_EMAIL")
        or _first_from_files(API_KEYS_DIR / "Unpaywall.txt", DESKTOP / "Unpaywall.txt")
        or "anonymous@example.invalid"
    )
    openalex_key = os.environ.get("OPENALEX_API_KEY") or _first_from_files(API_KEYS_DIR / "openalex.txt", DESKTOP / "openalex.txt")
    core_key = os.environ.get("CORE_API_KEY") or _first_from_files(API_KEYS_DIR / "core_api.txt", DESKTOP / "core_api.txt")
    return {
        "legal_only": True,
        "scihub_enabled": False,
        "libgen_enabled": False,
        "scibban_enabled": False,
        "use_tor_for_scihub": False,
        "email": email,
        "openalex_api_key": openalex_key,
        "core_api_key": core_key,
        "network_proxy": "",
        "connect_timeout": 15,
        "read_timeout": 20,
        "request_delay_min": 0.0,
        "request_delay_max": 0.0,
        "fixed_request_delay_enabled": True,
        "parallel_sources": True,
        "parallel_probes": True,
        "min_pdf_size_bytes": 10_000,
        "max_unpaywall_candidates": 4,
        "auto_rename": False,
        "browser_sources_enabled": False,
        "instsci_enabled": False,
        "carsi_enabled": False,
        "ezproxy_enabled": False,
        "elsevier_api_key": "",
        "elsevier_insttoken": "",
    }


def _scansci_worker(queue: Any, doi: str, output_dir: str, use_institution: bool, config: dict[str, Any]) -> None:
    try:
        if str(SCANSCI_SRC) not in sys.path:
            sys.path.insert(0, str(SCANSCI_SRC))
        from scansci_pdf.config import DEFAULT_CONFIG
        from scansci_pdf.sources import download

        merged = dict(DEFAULT_CONFIG)
        merged.update(config)
        merged.update(
            {
                "legal_only": True,
                "scihub_enabled": False,
                "libgen_enabled": False,
                "scibban_enabled": False,
                "use_tor_for_scihub": False,
            }
        )
        result = download(
            doi,
            Path(output_dir),
            scihub_enabled=False,
            use_tor=False,
            use_instsci=bool(use_institution),
            bibtex=False,
            rename=False,
            _institutional=bool(use_institution),
            _config=merged,
        )
        queue.put(result or {"success": False, "error": "no result"})
    except Exception as exc:
        queue.put({"success": False, "error": f"{type(exc).__name__}: {exc}"})


class ScanSciLegalBackend:
    """Small legal-only facade around external_tools/scansci-pdf."""

    def __init__(self, *, config_overrides: dict[str, Any] | None = None) -> None:
        self.available = SCANSCI_SRC.exists()
        self.last_error = ""
        self.stats: dict[str, int] = {"requests": 0, "success": 0, "errors": 0}
        self.config = _default_config()
        if config_overrides:
            self.config.update(config_overrides)
        # Enforce the legal-only contract even if caller overrides config.
        self.config.update(
            {
                "legal_only": True,
                "scihub_enabled": False,
                "libgen_enabled": False,
                "scibban_enabled": False,
                "use_tor_for_scihub": False,
            }
        )

    def download_pdf(
        self,
        doi: str,
        output_dir: str | Path,
        *,
        use_institution: bool = False,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        doi = str(doi or "").strip()
        if not doi:
            return {"success": False, "error": "missing DOI"}
        if not self.available:
            return {"success": False, "error": f"ScanSci source tree not found: {SCANSCI_SRC}"}
        if str(SCANSCI_SRC) not in sys.path:
            sys.path.insert(0, str(SCANSCI_SRC))
        self.stats["requests"] += 1
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        before_files = {str(path.resolve()) for path in output_path.glob("*.pdf")}
        try:
            ctx = mp.get_context("spawn")
            queue: Any = ctx.Queue(maxsize=1)
            process = ctx.Process(
                target=_scansci_worker,
                args=(queue, doi, str(Path(output_dir)), bool(use_institution), dict(self.config)),
            )
            process.start()
            process.join(max(5, int(timeout_seconds or 60)))
            if process.is_alive():
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                late = self._find_late_pdf(doi, output_path, before_files)
                if late:
                    self.stats["success"] += 1
                    return {
                        "success": True,
                        "identifier": doi,
                        "doi": doi,
                        "file": str(late),
                        "source": "ScanSciLegal(late_capture)",
                        "cached": False,
                    }
                self.stats["errors"] += 1
                return {"success": False, "error": f"scansci_legal_timeout_after_{timeout_seconds}s"}
            result = queue.get_nowait() if not queue.empty() else {"success": False, "error": "scansci worker returned no result"}
            if not result.get("success"):
                late = self._find_late_pdf(doi, output_path, before_files)
                if late:
                    result = {
                        "success": True,
                        "identifier": doi,
                        "doi": doi,
                        "file": str(late),
                        "source": "ScanSciLegal(late_capture)",
                        "cached": False,
                    }
            source = str((result or {}).get("source") or "")
            if any(marker.lower() in source.lower() for marker in ["sci-hub", "scihub", "libgen", "scibban"]):
                self.stats["errors"] += 1
                return {
                    "success": False,
                    "error": f"blocked_non_legal_source:{source}",
                    "raw_result": result,
                }
            if result and result.get("success"):
                self.stats["success"] += 1
            else:
                self.stats["errors"] += 1
            return result or {"success": False, "error": "no result"}
        except Exception as exc:
            self.stats["errors"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"success": False, "error": self.last_error}

    @staticmethod
    def _find_late_pdf(doi: str, output_dir: Path, before_files: set[str]) -> Path | None:
        safe_a = doi.lower().replace("/", "_")
        safe_b = doi.lower().replace("/", "-")
        candidates: list[Path] = []
        for path in output_dir.glob("*.pdf"):
            if str(path.resolve()) in before_files:
                continue
            name = path.name.lower()
            if safe_a in name or safe_b in name:
                try:
                    if path.stat().st_size > 10_000:
                        candidates.append(path)
                except OSError:
                    pass
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_size)
