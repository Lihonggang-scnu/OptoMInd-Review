"""Marker backend — PDF to Markdown/JSON.

Marker (https://github.com/VikParuchuri/marker) converts PDFs to clean Markdown.
Install: py -3.11 -m pip install marker-pdf
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class MarkerBackend:
    """PDF-to-Markdown converter using Marker."""

    def __init__(self) -> None:
        self.available = False
        self._marker_imported = False
        try:
            import importlib
            importlib.import_module("marker")
            self._marker_imported = True
            self.available = True
        except ImportError:
            pass
        self.stats: Dict[str, int] = {"parsed": 0, "errors": 0}

    def parse(self, pdf_path: str, output_dir: str | None = None) -> Optional[Dict[str, Any]]:
        """Convert PDF to Markdown. Returns metadata dict."""
        if not self.available:
            return None
        try:
            from marker.converters.pdf import PdfConverter
            from marker.models import create_model_dict

            converter = PdfConverter(artifact_dict=create_model_dict())
            rendered = converter(pdf_path)
            text = rendered.markdown if hasattr(rendered, "markdown") else str(rendered)
            self.stats["parsed"] += 1
            return {
                "source_path": pdf_path,
                "format": "markdown",
                "text": text,
                "parser": "marker",
            }
        except Exception:
            self.stats["errors"] += 1
            return None

    def check_status(self) -> Dict[str, Any]:
        return {"available": self.available, "install_command": "py -3.11 -m pip install marker-pdf", "note": "Marker may download model weights on first run."}
