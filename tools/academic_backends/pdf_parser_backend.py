"""PDF text parser using PyMuPDF (fitz).

Extracts text from PDFs and splits into evidence-ready chunks.
PyMuPDF must be installed: py -3.11 -m pip install pymupdf
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class PdfParserBackend:
    """Parse PDFs with PyMuPDF and produce structured chunks."""

    def __init__(self) -> None:
        self.available = False
        self._fitz = None
        try:
            import fitz  # type: ignore
            self._fitz = fitz
            self.available = True
        except ImportError:
            pass
        self.stats: Dict[str, int] = {"pdfs_parsed": 0, "chunks_generated": 0, "errors": 0}

    def parse_pdf(self, pdf_path: str) -> Optional[Dict[str, Any]]:
        """Extract full text and metadata from a PDF. Returns dict with text/metadata."""
        if not self.available:
            self.stats["errors"] += 1
            return None

        path = Path(pdf_path)
        if not path.exists():
            self.stats["errors"] += 1
            return None

        try:
            doc = self._fitz.open(str(path))
            meta = doc.metadata or {}

            pages_text: List[Dict[str, Any]] = []
            full_text_parts: List[str] = []

            for page_num, page in enumerate(doc):
                text = page.get_text("text") or ""
                blocks = page.get_text("blocks")
                para_count = len([b for b in blocks if b[6] == 0]) if blocks else 0

                pages_text.append({
                    "page_number": page_num + 1,
                    "char_count": len(text),
                    "word_count_estimate": len(text.split()),
                    "paragraph_estimate": para_count,
                })
                full_text_parts.append(text)

            doc.close()
            full_text = "\n\n".join(full_text_parts)
            self.stats["pdfs_parsed"] += 1

            return {
                "source_path": str(path.resolve()),
                "total_pages": len(pages_text),
                "total_chars": sum(p["char_count"] for p in pages_text),
                "total_words_estimate": sum(p["word_count_estimate"] for p in pages_text),
                "pages": pages_text,
                "full_text": full_text,
                "pdf_metadata": {
                    "title": meta.get("title", ""),
                    "author": meta.get("author", ""),
                    "subject": meta.get("subject", ""),
                    "creator": meta.get("creator", ""),
                    "format": meta.get("format", ""),
                },
            }
        except Exception:
            self.stats["errors"] += 1
            return None

    def split_text_into_chunks(
        self,
        text: str,
        source_id: str,
        chunk_size: int = 2000,
        overlap: int = 200,
    ) -> List[Dict[str, Any]]:
        """Split text into overlapping chunks suitable for evidence extraction."""
        chunks: List[Dict[str, Any]] = []
        if not text.strip():
            return chunks

        paragraphs = re.split(r'\n\s*\n', text)
        current_chunk: List[str] = []
        current_size = 0
        para_index = 0
        chunk_index = 0
        page_estimate = 1

        while para_index < len(paragraphs):
            para = paragraphs[para_index].strip()
            if not para:
                para_index += 1
                continue

            if current_size + len(para) > chunk_size and current_chunk:
                chunk_text = "\n\n".join(current_chunk)
                chunk_id = f"{source_id}_chunk_{chunk_index:04d}"
                chunks.append({
                    "chunk_id": chunk_id,
                    "source_id": source_id,
                    "chunk_index": chunk_index,
                    "text": chunk_text,
                    "char_count": len(chunk_text),
                    "token_estimate": len(chunk_text.split()),
                    "paragraph_count": len(current_chunk),
                    "page_start": max(1, page_estimate - max(1, len(current_chunk) // 3)),
                    "page_end": page_estimate,
                    "page_estimate_start": max(1, page_estimate - max(1, len(current_chunk) // 3)),
                    "page_estimate_end": page_estimate,
                    "section_guess": self._guess_section(current_chunk[0]) if current_chunk else "",
                    "extraction_method": "pymupdf",
                })
                chunk_index += 1
                if overlap > 0 and len(current_chunk) > 1:
                    overlap_para = current_chunk[-1] if current_chunk else ""
                    current_chunk = [overlap_para] if overlap_para else []
                    current_size = len(overlap_para)
                else:
                    current_chunk = []
                    current_size = 0

            current_chunk.append(para)
            current_size += len(para)
            para_index += 1

        if current_chunk:
            chunk_text = "\n\n".join(current_chunk)
            chunk_id = f"{source_id}_chunk_{chunk_index:04d}"
            chunks.append({
                "chunk_id": chunk_id,
                "source_id": source_id,
                "chunk_index": chunk_index,
                "text": chunk_text,
                "char_count": len(chunk_text),
                "token_estimate": len(chunk_text.split()),
                "paragraph_count": len(current_chunk),
                "page_start": max(1, page_estimate - max(1, len(current_chunk) // 3)),
                "page_end": page_estimate,
                "page_estimate_start": max(1, page_estimate - max(1, len(current_chunk) // 3)),
                "page_estimate_end": page_estimate,
                "section_guess": self._guess_section(current_chunk[0]) if current_chunk else "",
                "extraction_method": "pymupdf",
            })

        self.stats["chunks_generated"] += len(chunks)
        return chunks

    def write_chunks(self, chunks: List[Dict[str, Any]], output_dir: str) -> Optional[str]:
        """Write chunks to a JSON file in the workspace."""
        if not chunks:
            return None
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        source_id = chunks[0].get("source_id", "unknown")
        path = out / f"{source_id.replace(':', '_').replace('/', '_')}_chunks.json"
        path.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path.resolve())

    @staticmethod
    def _guess_section(text: str) -> str:
        lowered = text.lower()[:200]
        if any(w in lowered for w in ["abstract", "摘要"]):
            return "abstract"
        if any(w in lowered for w in ["introduction", "引言", "background"]):
            return "introduction"
        if any(w in lowered for w in ["method", "experiment", "方法", "实验"]):
            return "methods"
        if any(w in lowered for w in ["result", "discussion", "结果", "讨论", "analysis"]):
            return "results"
        if any(w in lowered for w in ["conclusion", "结论", "summary"]):
            return "conclusion"
        if any(w in lowered for w in ["reference", "bibliography", "参考文献"]):
            return "references"
        return "body"


def parse_pdf_with_pymupdf(pdf_path: str) -> Optional[Dict[str, Any]]:
    """Convenience function: parse a single PDF."""
    parser = PdfParserBackend()
    if not parser.available:
        return None
    return parser.parse_pdf(pdf_path)
