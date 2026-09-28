"""PaperQA2 adapter skeleton.

PaperQA2 (https://github.com/whitead/paper-qa) is a local RAG system
for academic papers. It can index a directory of PDFs and answer questions
based on cited passages.

This is a SKELETON adapter. PaperQA2 requires:
- pip install paper-qa
- An LLM backend (OpenAI API or local model)
- A directory of PDFs to index

Strategy for OptoMind:
- PaperQA2 can serve as a local literature RAG for cited summary retrieval.
- Its answers should inform EvidenceAgent but NOT become EvidenceItem directly.
- EvidenceAgent must still verify claims against SourceRecord chunks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class PaperQABackend:
    """Skeleton adapter for PaperQA2 integration.

    When installed and configured:
    1. Index PDFs from literature_workspace/pdfs/
    2. Query the index for specific scientific questions
    3. Return cited passage summaries for EvidenceAgent review

    NOT YET ACTIVE — requires 'paper-qa' package installation and LLM backend.
    """

    def __init__(self) -> None:
        self.available = False
        self._paperqa = None
        try:
            import paperqa  # type: ignore
            self._paperqa = paperqa
            self.available = True
        except ImportError:
            pass

    def check_status(self) -> Dict[str, Any]:
        return {
            "installed": self.available,
            "package_name": "paper-qa",
            "install_command": "py -3.11 -m pip install paper-qa",
            "requires_llm": True,
            "llm_providers": ["openai", "anthropic", "local"],
            "recommended_usage": (
                "Use PaperQA2 as a local RAG for cited passage retrieval. "
                "Its answers should inform EvidenceAgent, not replace it. "
                "EvidenceAgent must verify claims against SourceRecord chunks."
            ),
        }

    def index_pdfs(self, pdf_dir: str) -> Dict[str, Any]:
        """Index a directory of PDFs. Returns status dict."""
        if not self.available:
            return {"status": "unavailable", "reason": "paper-qa not installed"}
        return {"status": "adapter_only", "reason": "Not yet implemented. Requires LLM configuration."}

    def query(self, question: str) -> Dict[str, Any]:
        """Query the indexed papers. Returns answer with citations."""
        if not self.available:
            return {"status": "unavailable", "reason": "paper-qa not installed"}
        return {"status": "adapter_only", "reason": "Not yet implemented."}
