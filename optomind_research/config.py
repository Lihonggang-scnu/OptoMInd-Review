"""Central configuration and secret discovery for OptoMind Research."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional

from config.secret_pool import choose_secret, secret_candidates


PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_KEYS_DIR = PROJECT_ROOT / "api_keys"
DESKTOP = Path.home() / "Desktop"


SECRET_FILES: Dict[str, tuple[str, ...]] = {
    "DASHSCOPE_API_KEY": ("qwen-api-key.txt",),
    "TAVILY_API_KEY": ("tavily_api_key.txt",),
    "JINA_API_KEY": ("Jina_api.txt", "jina_api.txt"),
    "FIRECRAWL_API_KEY": ("firecrawl.txt",),
    "SERPER_API_KEY": ("serper-api-key.txt",),
    "BRAVE_API_KEY": ("brave_key.txt",),
    "CORE_API_KEY": ("core_api.txt",),
    "SEMANTIC_SCHOLAR_API_KEY": ("semantic-scholar-api-key.txt",),
    "UNPAYWALL_EMAIL": ("Unpaywall.txt", "unpaywall.txt"),
    "CONTACT_EMAIL": ("Unpaywall.txt", "unpaywall.txt"),
    "OPENALEX_API_KEY": ("openalex.txt",),
    "OPENALEX_API_KEYS": ("openalex.txt",),
}


def load_secret(name: str, filenames: Iterable[str] = ()) -> Optional[str]:
    """Randomly choose a secret from env/project api_keys/legacy desktop files."""
    candidate = choose_secret([name], filenames or SECRET_FILES.get(name, ()))
    if not candidate:
        return None
    os.environ[name] = candidate.value
    return candidate.value


def load_secret_candidates(name: str, filenames: Iterable[str] = ()) -> list[str]:
    """Return all configured values without logging or masking them."""
    return [item.value for item in secret_candidates([name], filenames or SECRET_FILES.get(name, ()))]


def configure_secret_environment() -> Dict[str, bool]:
    """Populate environment variables without copying or logging secret values."""
    status: Dict[str, bool] = {}
    for name, files in SECRET_FILES.items():
        status[name] = bool(load_secret(name, files))
    qwen_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if qwen_key:
        os.environ.setdefault("QWEN_API_KEY", qwen_key)
    return status


@dataclass
class ResearchSettings:
    project_root: Path = PROJECT_ROOT
    output_root: Path = PROJECT_ROOT / "outputs" / "research_sessions"
    local_document_roots: list[Path] = field(default_factory=list)
    max_rounds: int = 3
    min_recursive_rounds: int = 2
    max_queries_per_round: int = 12
    results_per_backend: int = 5
    max_sources: int = 50
    max_fulltext_sources: int = 28
    max_chunks: int = 280
    chunks_per_claim: int = 8
    chunk_chars: int = 1800
    chunk_overlap: int = 240
    min_evidence_items: int = 6
    max_workers: int = 10
    embedding_model: str = "text-embedding-v3"
    report_model_tier: str = "premium_model"
    planner_model_tier: str = "premium_model"
    extraction_model_tier: str = "advanced_model"
    screening_model_tier: str = "cheap_model"
    screening_refine_model_tier: str = "standard_model"
    image_model: str = "qwen-image-2.0"
    enable_embeddings: bool = True
    generate_images: bool = False
    default_backends: list[str] = field(
        default_factory=lambda: [
            "tavily",
            "serper",
            "brave",
            "core",
            "arxiv",
            "crossref",
            "openalex",
            "semantic_scholar_public",
            "duckduckgo",
        ]
    )

    def __post_init__(self) -> None:
        configure_secret_environment()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.local_document_roots = [Path(p).resolve() for p in self.local_document_roots]

    def capability_status(self) -> Dict[str, bool]:
        secret_status = configure_secret_environment()
        return {
            "qwen_llm": secret_status.get("DASHSCOPE_API_KEY", False),
            "qwen_embeddings": secret_status.get("DASHSCOPE_API_KEY", False),
            "qwen_image": secret_status.get("DASHSCOPE_API_KEY", False),
            "tavily": secret_status.get("TAVILY_API_KEY", False),
            "jina_reader": secret_status.get("JINA_API_KEY", False),
            "firecrawl": secret_status.get("FIRECRAWL_API_KEY", False),
            "serper": secret_status.get("SERPER_API_KEY", False),
            "brave": secret_status.get("BRAVE_API_KEY", False),
            "core": secret_status.get("CORE_API_KEY", False),
            "semantic_scholar": secret_status.get("SEMANTIC_SCHOLAR_API_KEY", False),
            "unpaywall": bool(os.environ.get("UNPAYWALL_EMAIL") or os.environ.get("CONTACT_EMAIL")),
            "openalex_content": bool(os.environ.get("OPENALEX_API_KEY") or os.environ.get("OPENALEX_API_KEYS")),
            "github": bool(os.environ.get("GITHUB_TOKEN")),
            "zotero": bool(os.environ.get("ZOTERO_API_KEY") and os.environ.get("ZOTERO_LIBRARY_ID")),
        }
