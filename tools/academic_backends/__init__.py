"""Academic literature retrieval backends for OptoMind.

arXiv, Crossref, OpenAlex, Semantic Scholar, Unpaywall, CORE, GitHub,
local import, PDF parsing, GROBID, Docling, Marker, Unstructured,
PaperQA, institutional access, and chart extraction.
"""

from .arxiv_backend import ArxivBackend
from .chart_extraction_backend import ChartExtractionBackend
from .core_backend import CoreBackend
from .crossref_backend import CrossrefBackend
from .docling_backend import DoclingBackend
from .github_backend import GitHubBackend
from .grobid_backend import GrobidBackend
from .institutional_access_backend import InstitutionalAccessBackend
from .local_import_backend import LocalImportBackend
from .marker_backend import MarkerBackend
from .openalex_backend import OpenAlexBackend
from .paperqa_backend import PaperQABackend
from .pdf_parser_backend import PdfParserBackend
from .semantic_scholar_backend import SemanticScholarBackend
from .unpaywall_backend import UnpaywallBackend
from .unstructured_backend import UnstructuredBackend

__all__ = [
    "ArxivBackend",
    "ChartExtractionBackend",
    "CoreBackend",
    "CrossrefBackend",
    "DoclingBackend",
    "GitHubBackend",
    "GrobidBackend",
    "InstitutionalAccessBackend",
    "LocalImportBackend",
    "MarkerBackend",
    "OpenAlexBackend",
    "PaperQABackend",
    "PdfParserBackend",
    "SemanticScholarBackend",
    "UnpaywallBackend",
    "UnstructuredBackend",
]
