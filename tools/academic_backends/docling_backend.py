"""Docling backend — structured document conversion.

Docling converts PDF/DOCX/PPTX/HTML to unified JSON/Markdown.
Install: py -3.11 -m pip install docling
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class DoclingBackend:
    """Structured document parser using Docling (IBM Research)."""

    def __init__(self) -> None:
        self.available = False
        self._docling = None
        try:
            from docling.document_converter import DocumentConverter
            self._docling = DocumentConverter
            self.available = True
        except ImportError:
            pass
        self.stats: Dict[str, int] = {"parsed": 0, "errors": 0}

    def parse(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Parse a document and return structured content."""
        if not self.available:
            return None
        try:
            converter = self._docling()
            result = converter.convert(file_path)
            doc = result.document
            self.stats["parsed"] += 1
            return {
                "source_path": file_path,
                "format": "docling_json",
                "text": doc.export_to_text() if hasattr(doc, "export_to_text") else str(doc),
                "markdown": doc.export_to_markdown() if hasattr(doc, "export_to_markdown") else "",
                "parser": "docling",
            }
        except Exception:
            self.stats["errors"] += 1
            return None

    def check_status(self) -> Dict[str, Any]:
        return {"available": self.available, "install_command": "py -3.11 -m pip install docling"}
