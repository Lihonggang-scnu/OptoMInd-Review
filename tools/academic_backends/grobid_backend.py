"""Small, local GROBID HTTP adapter.

The acquisition layer treats GROBID as an optional parser service. A Docker
installation is one way to run it, but the client only depends on the HTTP
endpoint so an already-running service, native installation, or CI fixture is
equally valid. PDF parsing is serialized here; network acquisition can still
use several workers around it.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


GROBID_DEFAULT_URL = "http://127.0.0.1:8070"
GROBID_IMAGE = "grobid/grobid:0.9.1-crf"
GROBID_VERSION = "0.9.1"


class GrobidBackend:
    """Parse PDFs through a local GROBID service when it is healthy."""

    def __init__(
        self,
        service_url: str | None = None,
        *,
        health_timeout: float = 3.0,
        parse_timeout: float = 120.0,
    ) -> None:
        self.service_url = (service_url or os.environ.get("GROBID_URL") or GROBID_DEFAULT_URL).rstrip("/")
        self.health_timeout = max(0.2, float(health_timeout))
        self.parse_timeout = max(1.0, float(parse_timeout))
        self._parse_lock = threading.Lock()
        self._docker_available = shutil.which("docker") is not None
        self.available = self._health_check()
        self.service_version = self._read_version() if self.available else "unknown"
        self.last_error = ""
        self.stats: Dict[str, int] = {"health_checks": 1, "parse_requests": 0, "parse_errors": 0}

    def _health_check(self) -> bool:
        try:
            request = urllib.request.Request(f"{self.service_url}/api/isalive", headers={"Accept": "text/plain"})
            with urllib.request.urlopen(request, timeout=self.health_timeout) as response:
                if int(getattr(response, "status", 200) or 200) != 200:
                    return False
                body = response.read(256).strip().lower()
                return not body or body in {b"true", b"1", b"ok", b"alive"}
        except Exception:
            return False

    def _read_version(self) -> str:
        try:
            request = urllib.request.Request(f"{self.service_url}/api/version", headers={"Accept": "text/plain"})
            with urllib.request.urlopen(request, timeout=self.health_timeout) as response:
                value = response.read(256).decode("utf-8", errors="replace").strip()
                if value.startswith("{"):
                    try:
                        payload = json.loads(value)
                        value = str(payload.get("version") or payload.get("service_version") or value)
                    except Exception:
                        pass
                return value or "unknown"
        except Exception:
            return "unknown"

    def refresh(self) -> bool:
        self.available = self._health_check()
        self.service_version = self._read_version() if self.available else "unknown"
        self.stats["health_checks"] = self.stats.get("health_checks", 0) + 1
        return self.available

    def parse_pdf(self, pdf_path: str, *, deadline: float | None = None) -> Optional[Dict[str, Any]]:
        """Return raw derived TEI and provenance, or ``None`` on parser failure."""

        pdf_file = Path(pdf_path)
        if not pdf_file.is_file():
            self.last_error = "pdf_missing"
            return None
        if not self.available and not self.refresh():
            self.last_error = "grobid_unavailable"
            return None
        try:
            pdf_bytes = pdf_file.read_bytes()
        except OSError:
            self.last_error = "pdf_read_failed"
            return None
        if not pdf_bytes.startswith(b"%PDF"):
            self.last_error = "pdf_magic_missing"
            return None

        boundary = b"----OptoMindGrobidBoundary"
        filename = pdf_file.name.encode("utf-8", "replace")
        body = b"--" + boundary + b"\r\n"
        body += b'Content-Disposition: form-data; name="input"; filename="' + filename + b'"\r\n'
        body += b"Content-Type: application/pdf\r\n\r\n" + pdf_bytes
        # GROBID expects one multipart field per coordinate target. Sending a
        # comma-joined value is accepted by some versions but silently yields
        # no coordinates on others.
        for coordinate_name in ("p", "figure", "ref", "formula", "biblStruct"):
            body += b"\r\n--" + boundary + b"\r\nContent-Disposition: form-data; name=\"teiCoordinates\"\r\n\r\n" + coordinate_name.encode("ascii")
        body += b"\r\n--" + boundary + b"--\r\n"
        request = urllib.request.Request(
            f"{self.service_url}/api/processFulltextDocument",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary.decode('ascii')}",
                "Accept": "application/xml",
            },
            method="POST",
        )
        self.stats["parse_requests"] = self.stats.get("parse_requests", 0) + 1
        try:
            wait_timeout = self.parse_timeout if deadline is None else max(0.01, deadline - time.monotonic())
            if not self._parse_lock.acquire(timeout=wait_timeout):
                self.last_error = "grobid_queue_deadline"
                return None
            try:
                remaining = self.parse_timeout if deadline is None else max(0.01, deadline - time.monotonic())
                with urllib.request.urlopen(request, timeout=min(self.parse_timeout, remaining)) as response:
                    if int(getattr(response, "status", 200) or 200) != 200:
                        raise RuntimeError(f"http_{getattr(response, 'status', 0)}")
                    tei_bytes = response.read()
            finally:
                self._parse_lock.release()
            head = tei_bytes[:4096].lower()
            if not tei_bytes or b"<tei" not in head:
                self.last_error = "grobid_empty_or_non_tei"
                return None
            self.last_error = ""
            return {
                "source_pdf": str(pdf_file),
                "tei_xml": tei_bytes.decode("utf-8", errors="replace"),
                "tei_bytes": tei_bytes,
                "format": "tei_xml",
                "parser": "grobid",
                "parser_version": self.service_version,
            }
        except Exception as exc:
            self.stats["parse_errors"] = self.stats.get("parse_errors", 0) + 1
            self.last_error = type(exc).__name__
            return None

    def check_status(self) -> Dict[str, Any]:
        return {
            "docker_available": self._docker_available,
            "grobid_running": bool(self.available),
            "service_url": self.service_url,
            "health_endpoint": f"{self.service_url}/api/isalive",
            "version": self.service_version,
            "stats": dict(self.stats),
            "last_error": self.last_error,
        }


def grobid_startup_instructions() -> str:
    """Return optional Docker instructions without making Docker a requirement."""

    has_docker = shutil.which("docker") is not None
    if not has_docker:
        return (
            "GROBID is optional. Start any compatible service at "
            f"{GROBID_DEFAULT_URL}, or install Docker Desktop and run: "
            f"docker run -d --name grobid -p 8070:8070 {GROBID_IMAGE}"
        )
    return (
        "GROBID HTTP client is ready; Docker is optional. If needed, run: "
        f"docker run -d --name grobid -p 8070:8070 {GROBID_IMAGE}"
    )
