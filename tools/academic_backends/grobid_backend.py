"""GROBID Docker adapter skeleton.

GROBID (GeneRation Of BIbliographic Data) extracts structured metadata,
references, and full-text TEI XML from scholarly PDFs.

Requirements: Docker installed and running.
Startup: docker run -d -p 8070:8070 lfoppiano/grobid:0.8.0
Health check: http://localhost:8070/api/isalive

This adapter is a SKELETON. It will be activated when the user starts GROBID.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


GROBID_DEFAULT_URL = "http://localhost:8070"
GROBID_IMAGE = "lfoppiano/grobid:0.8.0"


class GrobidBackend:
    """GROBID adapter — checks Docker, health-checks the service, and parses PDFs.

    Disabled by default. Requires Docker to be installed and GROBID running.
    """

    def __init__(self, service_url: str = GROBID_DEFAULT_URL) -> None:
        self.service_url = service_url.rstrip("/")
        self.available = False
        self._docker_available = shutil.which("docker") is not None
        if self._docker_available:
            self.available = self._health_check()

    def _health_check(self) -> bool:
        try:
            resp = urllib.request.urlopen(
                f"{self.service_url}/api/isalive", timeout=10
            )
            return resp.status == 200
        except Exception:
            return False

    def parse_pdf(self, pdf_path: str) -> Optional[Dict[str, Any]]:
        """Send PDF to GROBID for full-text TEI XML parsing. Returns structured result."""
        if not self.available:
            return None

        pdf_file = Path(pdf_path)
        if not pdf_file.exists():
            return None

        boundary = "----OptoMindGrobidBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="input"; '
            f'filename="{pdf_file.name}"\r\n'
            f"Content-Type: application/pdf\r\n\r\n"
            + pdf_file.read_bytes().decode("latin-1", errors="replace")
            + f"\r\n--{boundary}--\r\n"
        )

        req = urllib.request.Request(
            f"{self.service_url}/api/processFulltextDocument",
            data=body.encode("latin-1", errors="replace"),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/xml",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                xml_text = resp.read().decode("utf-8", errors="replace")
                return {
                    "source_pdf": str(pdf_path),
                    "tei_xml": xml_text,
                    "format": "tei_xml",
                    "parser": "grobid",
                }
        except Exception:
            return None

    def check_status(self) -> Dict[str, Any]:
        return {
            "docker_available": self._docker_available,
            "grobid_running": self.available,
            "service_url": self.service_url,
            "health_endpoint": f"{self.service_url}/api/isalive",
        }


def grobid_startup_instructions() -> str:
    """Return instructions for starting GROBID."""
    has_docker = shutil.which("docker") is not None
    if not has_docker:
        return (
            "GROBID requires Docker. Install Docker Desktop from https://www.docker.com/products/docker-desktop\n"
            "Then run: docker run -d -p 8070:8070 lfoppiano/grobid:0.8.0\n"
            "GROBID will be available at http://localhost:8070"
        )
    return (
        "Docker detected. Start GROBID with:\n"
        "  docker run -d -p 8070:8070 lfoppiano/grobid:0.8.0\n"
        "Wait ~30s for startup, then check: curl http://localhost:8070/api/isalive"
    )
