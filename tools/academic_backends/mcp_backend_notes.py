"""MCP server integration notes for AgentScope academic tooling.

MCP (Model Context Protocol) servers can provide structured access
to academic databases. This file documents compatible MCP servers
and how AgentScope can integrate with them.

## Compatible MCP Servers (Audit Notes)

### arxiv-mcp-server
- Repo: https://github.com/example/arxiv-mcp
- Status: NOT YET CLONED
- Function: Search arXiv, fetch metadata, download PDFs
- Note: Python-based. Would duplicate existing ArxivBackend functionality.

### paper-search MCP
- Repo: https://github.com/example/paper-search-mcp
- Status: NOT YET CLONED
- Function: Multi-backend paper search (Crossref, Semantic Scholar, OpenAlex)
- Note: Requires API keys for premium backends.

### Semantic Scholar MCP
- Status: NOT YET CLONED
- Function: Paper search + citation graph
- Note: Requires SEMANTIC_SCHOLAR_API_KEY for full access.

## AgentScope MCP Integration

AgentScope 2.0 supports MCP servers via:
1. MCP server config in agentscope_config.json
2. Tool registration via ToolRegistry
3. Agent access via tool_call messages

Example config snippet:
```json
{
  "mcp_servers": {
    "arxiv": {
      "command": "python",
      "args": ["-m", "arxiv_mcp_server"],
      "env": {}
    }
  }
}
```

## Current Recommendation

For OptoMind Phase 1, the native Python backends (ArxivBackend, CrossrefBackend,
etc.) provide the same functionality without MCP overhead. MCP servers should
be considered for Phase 2 if:
1. The team wants hot-swappable backends
2. Multiple services need unified tool interfaces
3. The system grows beyond single-process execution

## Security Notes
- Do NOT connect MCP servers that bypass paywalls
- Do NOT connect Sci-Hub or similar services
- Audit MCP server source code before connecting
"""

MCP_SERVER_NOTES = __doc__
