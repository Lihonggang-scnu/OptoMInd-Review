"""Optional AgentScope 2.0 browser-MCP adapter for public journal pages.

This adapter is disabled until both OPTOMIND_BROWSER_MCP_URL and
OPTOMIND_BROWSER_MCP_TOOL are configured. It never bypasses paywalls and never
marks browser-only snippets as verified literature unless a DOI is present.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List


def _configured_url() -> str:
    return os.environ.get("OPTOMIND_BROWSER_MCP_URL", "").strip()


def _configured_tool() -> str:
    return os.environ.get("OPTOMIND_BROWSER_MCP_TOOL", "").strip()


class BrowserMCPBackend:
    """Call a configured browser MCP search tool through AgentScope 2.0."""

    def __init__(self) -> None:
        self.url = _configured_url()
        self.tool_name = _configured_tool()
        self.enabled = bool(self.url and self.tool_name)
        self.adapter_only = not self.enabled
        self.last_error = ""

    def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            return asyncio.run(
                self._search_async(query, max_results=max_results),
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    async def _search_async(
        self,
        query: str,
        max_results: int,
    ) -> List[Dict[str, Any]]:
        from agentscope.mcp import HttpMCPConfig, MCPClient

        client = MCPClient(
            name="optomind_journal_browser",
            is_stateful=False,
            mcp_config=HttpMCPConfig(
                url=self.url,
                timeout=45.0,
            ),
            enable_tools=[self.tool_name],
            execution_timeout=60.0,
        )
        tool = await client.get_tool(self.tool_name)
        response = await tool(
            query=query,
            max_results=max_results,
            allowed_content="public_metadata_abstract_open_access",
        )
        payload = self._decode_tool_response(response)
        raw_results = (
            payload.get("results", [])
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(raw_results, list):
            return []
        return [
            self._normalize_result(result, query, index)
            for index, result in enumerate(
                raw_results[:max_results],
                start=1,
            )
            if isinstance(result, dict)
        ]

    @staticmethod
    def _decode_tool_response(response: Any) -> Any:
        if isinstance(response, (dict, list)):
            return response
        content = getattr(response, "content", None)
        texts: List[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("text"):
                    texts.append(str(block["text"]))
                elif getattr(block, "text", None):
                    texts.append(str(block.text))
        raw = "\n".join(texts).strip()
        if not raw:
            return {}
        return json.loads(raw)

    @staticmethod
    def _normalize_result(
        result: Dict[str, Any],
        query: str,
        index: int,
    ) -> Dict[str, Any]:
        title = str(result.get("title") or "").strip()
        url = str(
            result.get("source_url")
            or result.get("url")
            or result.get("link")
            or ""
        ).strip()
        doi = str(result.get("doi") or "").strip()
        snippet = str(
            result.get("abstract")
            or result.get("snippet")
            or result.get("abstract_or_snippet")
            or ""
        ).strip()
        return {
            "source_id": (
                f"browser_mcp:{doi}"
                if doi
                else f"browser_mcp:{index}:{title[:80]}"
            ),
            "title": title,
            "authors": result.get("authors", []),
            "year": result.get("year"),
            "doi": doi,
            "url_or_doi": (
                f"https://doi.org/{doi}" if doi else url
            ),
            "arxiv_id": "",
            "semantic_scholar_paper_id": "",
            "openalex_id": "",
            "source_url": url,
            "pdf_url": str(result.get("pdf_url") or ""),
            "journal_or_venue": str(
                result.get("journal_or_venue")
                or result.get("journal")
                or ""
            ),
            "abstract_or_snippet": snippet,
            "query": query,
            "retrieval_method": "agentscope_browser_mcp",
            "backend": "browser_mcp",
            "verification_status": (
                "verified" if doi else "unverified"
            ),
            "evidence_extraction_ready": bool(
                doi and len(snippet) >= 80
            ),
            "relevance_score": 0.0,
            "raw_metadata": {
                "public_page_only": True,
                "paywall_bypass": False,
            },
            "notes": (
                "Browser MCP result from a public page. DOI metadata "
                "must be verified before literature use."
            ),
        }
