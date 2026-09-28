"""Unstructured backend — general-purpose document parsing fallback.

Unstructured (https://github.com/Unstructured-IO/unstructured) parses:
PDF, DOCX, PPTX, HTML, images, email, etc.

Install: py -3.11 -m pip install "unstructured[pdf]"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class UnstructuredBackend:
    """General-purpose document parser using Unstructured library."""

    def __init__(self) -> None:
        self.available = False
        try:
            import importlib
            importlib.import_module("unstructured")
            self.available = True
        except ImportError:
            pass
        self.stats: Dict[str, int] = {"parsed": 0, "errors": 0}

    def parse_pdf(self, pdf_path: str) -> Optional[Dict[str, Any]]:
        """Parse PDF and return structured elements."""
        if not self.available:
            return None
        try:
            from unstructured.partition.pdf import partition_pdf

            elements = partition_pdf(filename=pdf_path)
            self.stats["parsed"] += 1
            return {
                "source_path": pdf_path,
                "element_count": len(elements),
                "text": "\n".join(str(e) for e in elements),
                "elements": [
                    {"type": type(e).__name__, "text": str(e)[:500]}
                    for e in elements[:20]
                ],
                "parser": "unstructured",
            }
        except Exception:
            self.stats["errors"] += 1
            return None

    def partition_text(self, text: str) -> List[Dict[str, Any]]:
        """Partition plain text into structured elements."""
        if not self.available:
            return []
        try:
            from unstructured.partition.text import partition_text as pt
            elements = pt(text=text)
            return [{"type": type(e).__name__, "text": str(e)} for e in elements]
        except Exception:
            return []

    def check_status(self) -> Dict[str, Any]:
        return {"available": self.available, "install_command": 'py -3.11 -m pip install "unstructured[pdf]"'}
