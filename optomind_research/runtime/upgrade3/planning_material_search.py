"""Local-first material search for review planning.

Work order 01 of ``docs/planning_work_orders/20260926_content_driven_retrieval``.

The review planner needs to answer concrete writing questions from material it
already owns *before* any external retrieval happens.  This module is a thin
adapter: it indexes the registered reading text of the current topic (document
snapshot blocks plus the A/B planning card plus existing directed readings) into
one SQLite FTS5 database, and answers a concrete natural-language question with
readable passages plus the program-managed paper identity and material entry
point.

Design constraints taken from the work order:

* No network, no model, no embedding purchase.  A lexical index is enough to
  prove that the local material actually contains the answer.
* The index is built once and can be extended incrementally; a repeated build
  must not create duplicate candidates for one paper.
* Every paper keeps an independent slot: several passages of one paper may be
  returned, but they never crowd out other papers.
* Chinese questions must be able to reach English material, so a small curated
  bilingual terminology map expands the query.
* The adapter deliberately does not accept a gateway or an acquirer, so it
  cannot reach the network even by accident.

Ranking is by inverse document frequency, not by raw term frequency.  A long
review that mentions every query word must not outrank a short passage that
actually reports the specific relation the question asks about; `CTLA-4` or
`Debaryomyces` therefore weigh far more than `microbiome`.

Nothing here decides whether the material is *sufficient*; that judgement is
work order 02.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "optomind.planning_material_search.v1"
INDEX_SCHEMA_VERSION = "optomind.planning_material_search.index.v2"

SEGMENT_CHAR_TARGET = 620
SEGMENT_CHAR_OVERLAP = 120
SNIPPET_CHARS = 220
BM25_CANDIDATES = 3000
MAX_FTS_CONCEPTS = 10
PAPER_SCORE_PASSAGES = 3
PROXIMITY_BONUS = 1.5
SEGMENT_COVERAGE_FLOOR = 0.75
PAPER_COVERAGE_FLOOR = 0.6
FTS_TABLE = "segment_fts"

#: Segment kinds that carry the strongest, most quotable local content.
KIND_WEIGHTS = {
    "document_block": 1.0,
    "directed_question": 1.15,
    "directed_content": 1.0,
    "card_key_finding": 1.05,
    "card_facet_contribution": 0.95,
    "card_contribution_limit": 0.95,
    "card_review_use": 0.85,
    "card_planning_summary": 0.8,
    "card_work_summary": 0.8,
    "card_approach": 0.7,
    "card_research_scope": 0.65,
    "card_scope_caution": 0.6,
    "card_open_question": 0.6,
    "card_topic_handle": 0.4,
}

#: Curated bilingual terminology.  Chinese review questions must be able to
#: reach English material without a translator model; the map is intentionally
#: small, reviewed, and stored in the repository instead of being learned.
TERM_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "短链脂肪酸": ("short-chain fatty acid", "short-chain fatty acids", "scfa", "scfas"),
    "丁酸": ("butyrate", "butyric acid", "butanoate"),
    "丙酸": ("propionate", "propionic acid"),
    "乙酸": ("acetate", "acetic acid"),
    "戊酸": ("valerate", "valeric acid"),
    "胆汁酸": ("bile acid", "bile acids"),
    "色氨酸": ("tryptophan",),
    "吲哚": ("indole", "indoles"),
    "肌苷": ("inosine",),
    "三甲胺": ("trimethylamine", "tmao"),
    "代谢物": ("metabolite", "metabolites"),
    "微生物组": ("microbiome", "microbiota"),
    "肠道菌群": ("gut microbiome", "gut microbiota", "intestinal microbiota"),
    "真菌": ("fungal", "fungi", "mycobiome", "mycobiota"),
    "病毒组": ("virome", "viral", "virus"),
    "噬菌体": ("phage", "bacteriophage"),
    "口腔": ("oral", "saliva", "salivary", "buccal"),
    "唾液": ("saliva", "salivary"),
    "免疫检查点": ("immune checkpoint", "checkpoint inhibitor", "checkpoint inhibitors"),
    "免疫治疗": ("immunotherapy", "immune checkpoint inhibitor", "ici"),
    "无进展生存期": ("progression-free survival", "pfs"),
    "总生存期": ("overall survival", "os"),
    "浓度": ("concentration", "concentrations", "level", "levels"),
    "剂量": ("dose", "dosage", "dose-dependent"),
    "粪菌移植": ("fecal microbiota transplantation", "fmt", "faecal microbiota transplant"),
    "益生菌": ("probiotic", "probiotics"),
    "随机对照": ("randomised controlled", "randomized controlled", "rct", "randomised", "randomized"),
    "三期": ("phase 3", "phase iii", "phase3"),
    "机制": ("mechanism", "mechanisms", "pathway", "pathways"),
    "调节性t细胞": ("regulatory t cell", "regulatory t cells", "treg", "tregs"),
    "抗生素": ("antibiotic", "antibiotics"),
    "质子泵抑制剂": ("proton pump inhibitor", "proton pump inhibitors", "ppi", "ppis"),
    "可重复性": ("reproducibility", "repeatability", "replication"),
    "标志物": ("biomarker", "biomarkers", "marker", "markers"),
    "肿瘤微环境": ("tumour microenvironment", "tumor microenvironment", "tme"),
    "表观遗传": ("epigenetic", "epigenetics", "histone", "hdac"),
    "受体": ("receptor", "receptors"),
    "树突状细胞": ("dendritic cell", "dendritic cells", "dc"),
}

#: Phrases that should survive tokenisation as one concept.
_PHRASE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[ -][A-Za-z0-9]+)*")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*|[\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EN_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "between", "by", "can", "could", "did",
    "do", "does", "for", "from", "had", "has", "have", "how", "in", "into", "is", "it",
    "its", "may", "might", "of", "on", "or", "should", "that", "the", "their", "them",
    "these", "this", "those", "to", "was", "were", "what", "when", "where", "which",
    "while", "who", "why", "will", "with", "would", "does", "vs", "vs.", "not",
}
_ZH_STOPWORDS = {"的", "了", "是", "在", "和", "与", "有", "对", "为", "吗", "呢", "哪", "些", "个", "如何", "什么", "哪些", "以及", "是否"}

# These sections describe the publication record rather than the study.  A
# few PDF extractors lose the heading, so the text checks below cover the
# same material when ``section_path`` is empty.
_NON_EVIDENCE_SECTION_MARKERS = (
    "reference", "bibliograph", "author contribution", "authors' contribution",
    "ethical", "ethics", "consent to participate", "data availability",
    "competing interest", "conflict of interest", "funding", "acknowledg",
)
_NON_EVIDENCE_TEXT_MARKERS = (
    "human ethics and consent to participate", "authors' contributions",
    "author contributions", "data availability statement", "competing interests",
    "conflict of interest", "springer nature remains neutral",
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
# A citation-shaped suffix is intentionally publisher/domain agnostic.  It is
# used only twice in one unlabelled block; one historical citation in prose is
# never enough to classify a passage as bibliography.
_BIBLIOGRAPHIC_CITATION_RE = re.compile(
    r"\b(?:19|20)\d{2}\s*[.;,]?\s*\d{1,4}\s*\(\s*\d{1,4}\s*\)\s*[:;,]?\s*\d{1,5}(?:\s*[-–]\s*\d{1,5})?"
)
_REFERENCE_VOLUME_PAGE_RE = re.compile(
    r"\b\d{1,4}\s+(?:\d{1,4}\s+)?\d{1,5}(?:\s*[-–]\s*\d{1,5})?\b"
)
_AUTHOR_LIST_RE = re.compile(r"\b[A-Z][a-z]{2,}\s+[A-Z][A-Za-z.]{0,4}\s*,")


def _is_non_evidence_material(text: str, section_path: Sequence[str] = ()) -> bool:
    """Identify publication metadata and bibliography entries.

    Section semantics are the primary signal.  The text fallback is purposely
    conservative: it only removes explicit declarations or bibliography-like
    records with repeated publication years/DOIs.  This leaves review prose
    that cites original studies available for retrieval, including prose in a
    PDF block whose heading was lost during extraction.
    """

    section = " / ".join(str(part) for part in section_path).casefold()
    if any(marker in section for marker in _NON_EVIDENCE_SECTION_MARKERS):
        return True
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    folded = body.casefold()
    # Metadata text can be concatenated to a useful result by PDF extraction.
    # Only a declaration at the beginning of the chunk is authoritative.
    leading = folded[:220]
    if any(leading.startswith(marker) for marker in _NON_EVIDENCE_TEXT_MARKERS):
        return True
    # Unlabelled flattened reference blocks are filtered only when they contain
    # at least two complete year/volume/pages citation shapes.  This avoids
    # deleting ordinary prose such as “In 2018 Smith et al... In 2020 Jones...”
    # or a single research paragraph that happens to include a DOI.
    if len(_BIBLIOGRAPHIC_CITATION_RE.findall(body)) >= 2:
        return True
    return False


def _unlabelled_reference_likelihood(text: str, section_path: Sequence[str] = ()) -> float:
    """Return a cautious ranking penalty for flattened citation fragments.

    This is deliberately separate from the hard filter: malformed PDF text is
    too unreliable to discard when it lacks a section heading.  The penalty
    only applies when repeated years and a volume/page-like numeric run occur
    together, a shape uncommon in ordinary historical review prose.
    """

    if section_path or _is_non_evidence_material(text, section_path):
        return 1.0
    body = str(text or "")
    if len(_YEAR_RE.findall(body)) < 2:
        # Flattened reference rows can lose years, but a long comma-separated
        # author list ending in ``et al.`` is still a useful generic signal.
        return 0.25 if (" et al" in body.casefold() and len(_AUTHOR_LIST_RE.findall(body)) >= 3) else 1.0
    if _REFERENCE_VOLUME_PAGE_RE.search(body):
        return 0.25
    return 0.25 if (" et al" in body.casefold() and len(_AUTHOR_LIST_RE.findall(body)) >= 3) else 1.0


class PlanningMaterialSearchError(ValueError):
    """Invalid index, material, or query input."""


# --------------------------------------------------------------------------
# Text normalisation and query expansion
# --------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Lexical tokens for both English and Chinese text.

    English words keep internal hyphens (``ctla-4``, ``pd-1``); CJK runs become
    single characters so that a Chinese question can be matched by the FTS5
    ``unicode61`` analyser without a segmentation dependency.
    """

    return _TOKEN_RE.findall(str(text or "").casefold())


def _base_form(token: str) -> set[str]:
    """Very small English variant generator (plural / gerund / past)."""

    forms = {token}
    if len(token) > 4:
        if token.endswith("ies"):
            forms.add(token[:-3] + "y")
        if token.endswith("es") and not token.endswith("ses"):
            forms.add(token[:-2])
        if token.endswith("s") and not token.endswith("ss"):
            forms.add(token[:-1])
        if token.endswith("ing"):
            forms.add(token[:-3])
            forms.add(token[:-3] + "e")
        if token.endswith("ed"):
            forms.add(token[:-2])
            forms.add(token[:-1])
    if token.endswith("our"):
        forms.add(token[:-3] + "or")
    if token.endswith("ise"):
        forms.add(token[:-3] + "ize")
    return {form for form in forms if form}


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


@dataclass(frozen=True)
class QueryTerm:
    """One query concept and the lexical forms that may realise it."""

    concept: str
    forms: tuple[str, ...]
    source: str  # "question" | "expansion"

    def to_dict(self) -> dict[str, Any]:
        return {"concept": self.concept, "forms": list(self.forms), "source": self.source}


def expand_query(question: str) -> list[QueryTerm]:
    """Turn a concrete question into concepts plus lexical variants."""

    text = str(question or "").strip()
    if not text:
        raise PlanningMaterialSearchError("question_required")
    terms: list[QueryTerm] = []
    used: set[str] = set()
    folded = text.casefold()

    for zh, english in TERM_EXPANSIONS.items():
        if zh in folded:
            forms = _dedupe(
                form
                for phrase in english
                for form in _phrase_forms(phrase)
            )
            if forms:
                terms.append(QueryTerm(concept=zh, forms=tuple(forms), source="expansion"))
                used.update(forms)

    for phrase in _PHRASE_RE.findall(text):
        words = [w for w in tokenize(phrase) if w not in _EN_STOPWORDS]
        if not words:
            continue
        if len(words) == 1 and words[0] in used:
            continue
        forms = _dedupe(form for word in words for form in _base_form(word))
        if not forms:
            continue
        concept = " ".join(words)
        if concept in {term.concept for term in terms}:
            continue
        terms.append(QueryTerm(concept=concept, forms=tuple(forms), source="question"))
        used.update(forms)
    return terms


def concept_term(concept: str) -> QueryTerm:
    """Build one query concept from a literal phrase, using the term map.

    Callers use this to attach a hard requirement to a search (for example
    "the passage must talk about the tumour microenvironment"), so lexical
    overlap on generic words cannot pass for an answer.
    """

    phrase = str(concept or "").strip()
    if not phrase:
        raise PlanningMaterialSearchError("concept_required")
    if phrase in TERM_EXPANSIONS:
        forms = _dedupe(
            form for item in TERM_EXPANSIONS[phrase] for form in _phrase_forms(item)
        )
        return QueryTerm(concept=phrase, forms=tuple(forms), source="expansion")
    words = [word for word in tokenize(phrase) if word not in _EN_STOPWORDS]
    if not words:
        raise PlanningMaterialSearchError("concept_has_no_searchable_terms:" + phrase)
    forms = _dedupe(form for word in words for form in _base_form(word))
    return QueryTerm(concept=phrase, forms=tuple(forms), source="question")


def _phrase_forms(phrase: str) -> list[str]:
    words = tokenize(phrase)
    if not words:
        return []
    if len(words) == 1:
        return sorted(_base_form(words[0]))
    # A multi-word expansion is used as an FTS phrase; keep it verbatim plus a
    # singular variant of the last word so "fatty acids" also finds "fatty acid".
    forms = [phrase.casefold()]
    if words[-1].endswith("s"):
        forms.append(" ".join([*words[:-1], words[-1][:-1]]))
    return forms


def _fts_literal(form: str) -> str:
    parts = [part for part in re.split(r"\s+", form.strip()) if part]
    if not parts:
        return ""
    if len(parts) == 1:
        quoted = '"' + parts[0].replace('"', "") + '"'
        return quoted
    return '"' + " ".join(part.replace('"', "") for part in parts) + '"'


def fts_query(terms: Sequence[QueryTerm]) -> str:
    """Build an FTS5 MATCH expression: concepts OR-ed, forms OR-ed inside."""

    groups: list[str] = []
    for term in terms:
        literals = _dedupe(_fts_literal(form) for form in term.forms)
        if literals:
            groups.append("(" + " OR ".join(literals) + ")")
    if not groups:
        raise PlanningMaterialSearchError("query_has_no_searchable_terms")
    return " OR ".join(groups)


# --------------------------------------------------------------------------
# Segment splitting
# --------------------------------------------------------------------------


def split_reading_text(text: str, *, target: int = SEGMENT_CHAR_TARGET, overlap: int = SEGMENT_CHAR_OVERLAP) -> list[str]:
    """Split one block of useful prose into readable, overlapping segments."""

    body = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not body:
        return []
    if len(body) <= target:
        return [body]

    sentences = re.split(r"(?<=[.!?。！？])\s+", body)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > target * 2:
            # A very long sentence (usually a merged table row) is hard-cut.
            if current:
                chunks.append(current.strip())
                current = ""
            step = target - overlap
            for start in range(0, len(sentence), max(1, step)):
                piece = sentence[start:start + target].strip()
                if piece:
                    chunks.append(piece)
                if start + target >= len(sentence):
                    break
            continue
        candidate = (current + " " + sentence).strip() if current else sentence
        if len(candidate) <= target:
            current = candidate
            continue
        if current:
            chunks.append(current.strip())
            tail = current[-overlap:] if overlap > 0 else ""
            current = (tail + " " + sentence).strip()
        else:
            current = sentence
        while len(current) > target * 1.5:
            chunks.append(current[:target].strip())
            current = current[target - overlap:].strip()
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


# --------------------------------------------------------------------------
# Material discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchIndexConfig:
    """Where the topic material lives and where the index may be written."""

    topic_id: str
    index_path: Path
    pool_path: Path
    identity_map_path: Path
    snapshot_roots: tuple[Path, ...] = ()
    directed_roots: tuple[Path, ...] = ()
    synthetic_card_roots: tuple[Path, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "index_path": str(self.index_path),
            "pool_path": str(self.pool_path),
            "identity_map_path": str(self.identity_map_path),
            "snapshot_roots": [str(p) for p in self.snapshot_roots],
            "directed_roots": [str(p) for p in self.directed_roots],
            "synthetic_card_roots": [str(p) for p in self.synthetic_card_roots],
        }


@dataclass
class PaperRecord:
    """Program-managed identity for one local paper."""

    paper_id: str
    source_handle: str
    title: str
    year: str
    doi: str
    card_path: str = ""
    snapshot_path: str = ""
    material_depth: str = ""
    identity_status: str = "pool_identity"
    pool_action: str = "already_in_pool"
    directed_reading_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "source_handle": self.source_handle,
            "title": self.title,
            "year": self.year,
            "doi": self.doi,
            "card_path": self.card_path,
            "snapshot_path": self.snapshot_path,
            "material_depth": self.material_depth,
            "identity_status": self.identity_status,
            "pool_action": self.pool_action,
            "directed_reading_path": self.directed_reading_path,
        }


@dataclass
class SearchHit:
    """One readable local passage answering (part of) a question."""

    paper_id: str
    source_handle: str
    title: str
    year: str
    doi: str
    segment_kind: str
    section_path: tuple[str, ...]
    text: str
    score: float
    match_terms: tuple[str, ...]
    material_depth: str
    reading_path: str
    card_path: str
    pool_action: str
    usable_fields: tuple[str, ...] = ()
    limits: tuple[str, ...] = ()
    best_sentence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "source_handle": self.source_handle,
            "title": self.title,
            "year": self.year,
            "doi": self.doi,
            "segment_kind": self.segment_kind,
            "section_path": list(self.section_path),
            "text": self.text,
            "best_sentence": self.best_sentence,
            "score": round(float(self.score), 6),
            "match_terms": list(self.match_terms),
            "material_depth": self.material_depth,
            "reading_path": self.reading_path,
            "card_path": self.card_path,
            "pool_action": self.pool_action,
            "usable_fields": list(self.usable_fields),
            "limits": list(self.limits),
        }


@dataclass
class SearchResult:
    """Search outcome for one question, including honest emptiness."""

    question: str
    query_terms: tuple[QueryTerm, ...]
    hits: list[SearchHit] = field(default_factory=list)
    candidate_papers: int = 0
    matched_papers: int = 0
    index_papers: int = 0
    index_segments: int = 0
    missing_material: tuple[str, ...] = ()
    not_matched_reason: str = ""
    external_calls: int = 0

    @property
    def found(self) -> bool:
        return bool(self.hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "query_terms": [term.to_dict() for term in self.query_terms],
            "hits": [hit.to_dict() for hit in self.hits],
            "candidate_papers": self.candidate_papers,
            "matched_papers": self.matched_papers,
            "index_papers": self.index_papers,
            "index_segments": self.index_segments,
            "missing_material": list(self.missing_material),
            "not_matched_reason": self.not_matched_reason,
            "external_calls": self.external_calls,
            "found": self.found,
        }


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class PlanningMaterialIndex:
    """SQLite FTS5 index over registered local material for one topic."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = bool(readonly)
        if self.readonly:
            if not self.path.is_file():
                raise PlanningMaterialSearchError("index_missing:" + str(self.path))
            self._conn = sqlite3.connect(f"file:{self.path.resolve().as_posix()}?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        if not self.readonly:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._create_schema()

    # -- lifecycle ---------------------------------------------------------

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS papers (
                paper_id TEXT PRIMARY KEY,
                source_handle TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                doi TEXT NOT NULL DEFAULT '',
                card_path TEXT NOT NULL DEFAULT '',
                snapshot_path TEXT NOT NULL DEFAULT '',
                material_depth TEXT NOT NULL DEFAULT '',
                identity_status TEXT NOT NULL DEFAULT '',
                pool_action TEXT NOT NULL DEFAULT '',
                directed_reading_path TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS segments (
                segment_id TEXT PRIMARY KEY,
                paper_id TEXT NOT NULL,
                segment_kind TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                section_path TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                char_start INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_segments_paper ON segments(paper_id, segment_kind, ordinal);
            CREATE VIRTUAL TABLE IF NOT EXISTS segment_fts USING fts5(
                text,
                segment_id UNINDEXED,
                paper_id UNINDEXED,
                segment_kind UNINDEXED,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE IF NOT EXISTS paper_terms (
                paper_id TEXT NOT NULL,
                card_terms TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (paper_id)
            );
            CREATE TABLE IF NOT EXISTS segment_keys (
                paper_id TEXT NOT NULL,
                segment_kind TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                PRIMARY KEY (paper_id, segment_kind, segment_id)
            );
            CREATE TABLE IF NOT EXISTS term_df (
                term TEXT PRIMARY KEY,
                doc_freq INTEGER NOT NULL
            );
            """
        )
        self._conn.execute(
            "INSERT INTO index_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (INDEX_SCHEMA_VERSION,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PlanningMaterialIndex":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- writing -----------------------------------------------------------

    def upsert_paper(self, record: PaperRecord) -> bool:
        """Insert or update one paper row. Returns True when it was new."""

        existing = self._conn.execute(
            "SELECT paper_id FROM papers WHERE paper_id=?", (record.paper_id,)
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO papers(paper_id, source_handle, title, year, doi, card_path,
                               snapshot_path, material_depth, identity_status, pool_action,
                               directed_reading_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(paper_id) DO UPDATE SET
                source_handle=excluded.source_handle,
                title=excluded.title,
                year=excluded.year,
                doi=excluded.doi,
                card_path=excluded.card_path,
                snapshot_path=excluded.snapshot_path,
                material_depth=excluded.material_depth,
                identity_status=excluded.identity_status,
                pool_action=excluded.pool_action,
                directed_reading_path=excluded.directed_reading_path
            """,
            (
                record.paper_id, record.source_handle, record.title, record.year,
                record.doi, record.card_path, record.snapshot_path,
                record.material_depth, record.identity_status, record.pool_action,
                record.directed_reading_path,
            ),
        )
        return existing is None

    def _delete_segment_ids(self, ids: Sequence[str], *, chunk: int = 400) -> int:
        removed = 0
        for start in range(0, len(ids), max(1, chunk)):
            batch = list(ids[start:start + max(1, chunk)])
            placeholders = ",".join("?" * len(batch))
            self._conn.execute(
                f"DELETE FROM segment_fts WHERE segment_id IN ({placeholders})", batch
            )
            self._conn.execute(
                f"DELETE FROM segments WHERE segment_id IN ({placeholders})", batch
            )
            removed += len(batch)
        return removed

    def add_segments(
        self,
        paper_id: str,
        segments: Sequence[Mapping[str, Any]],
        *,
        replace_kind: str | None = None,
    ) -> int:
        """Append segments for one paper.

        ``replace_kind`` removes earlier segments of that kind first, so a
        repeated index build cannot turn one paper into several candidates.
        """

        if replace_kind:
            old_ids = [row["segment_id"] for row in self._conn.execute(
                "SELECT segment_id FROM segment_keys WHERE paper_id=? AND segment_kind=?",
                (paper_id, replace_kind),
            )]
            self._delete_segment_ids(old_ids)
            self._conn.execute(
                "DELETE FROM segment_keys WHERE paper_id=? AND segment_kind=?",
                (paper_id, replace_kind),
            )
        rows = self._segment_rows(paper_id, segments)
        if rows:
            self._insert_segment_rows(rows)
        return len(rows)

    def _segment_rows(
        self, paper_id: str, segments: Sequence[Mapping[str, Any]]
    ) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        seen: set[str] = set()
        for position, row in enumerate(segments):
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            kind = str(row.get("segment_kind") or "document_block")
            ordinal = int(row.get("ordinal", position))
            section = " / ".join(str(item) for item in (row.get("section_path") or ()))
            segment_id = f"{paper_id}:{kind}:{ordinal}:{_sha1(text)[:12]}"
            if segment_id in seen:
                continue
            seen.add(segment_id)
            rows.append((
                segment_id, paper_id, kind, ordinal, section, text,
                int(row.get("char_start") or 0),
            ))
        return rows

    def _insert_segment_rows(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._conn.executemany(
            "INSERT INTO segments(segment_id, paper_id, segment_kind, ordinal, section_path, text, char_start)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(segment_id) DO UPDATE SET"
            " section_path=excluded.section_path, text=excluded.text",
            rows,
        )
        self._conn.executemany(
            "INSERT INTO segment_keys(paper_id, segment_kind, segment_id) VALUES (?,?,?)",
            [(row[1], row[2], row[0]) for row in rows],
        )
        self._conn.executemany(
            "INSERT INTO segment_fts(text, segment_id, paper_id, segment_kind) VALUES (?,?,?,?)",
            [(row[5], row[0], row[1], row[2]) for row in rows],
        )

    def bulk_replace_paper(self, paper_id: str, segments: Sequence[Mapping[str, Any]]) -> int:
        """Replace every indexed segment of one paper in a few batched writes.

        The full-topic build uses this instead of one call per segment kind:
        the deletion is driven by the ``segment_keys`` map and the inserts are
        batched, so rebuilding an unchanged index does not grow the database and
        cannot leave a paper behind as several candidates.
        """

        old_ids = [row["segment_id"] for row in self._conn.execute(
            "SELECT segment_id FROM segment_keys WHERE paper_id=?", (paper_id,)
        )]
        self._conn.execute("DELETE FROM segment_keys WHERE paper_id=?", (paper_id,))
        rows = self._segment_rows(paper_id, segments)
        keep = {row[0] for row in rows}
        stale = [segment_id for segment_id in old_ids if segment_id not in keep]
        self._delete_segment_ids(stale)
        if rows:
            self._insert_segment_rows(rows)
        return len(rows)

    def set_paper_terms(self, paper_id: str, terms: Iterable[str]) -> None:
        self._conn.execute(
            "INSERT INTO paper_terms(paper_id, card_terms) VALUES (?, ?) "
            "ON CONFLICT(paper_id) DO UPDATE SET card_terms=excluded.card_terms",
            (paper_id, " ".join(str(t) for t in terms if str(t).strip())),
        )

    def record_term_document_frequency(self) -> int:
        """Build the term -> document-frequency table used for ranking.

        Search weights each query concept by inverse document frequency, so a
        rare signal term ("ctla-4", "debaromyces") outranks a ubiquitous one
        ("microbiome").  The counts are computed from the indexed segment text
        rather than read out of an FTS5 shadow table, because the FTS5 term
        index only keeps the leaf pages of its b-tree and is therefore not a
        complete lexicon.
        """

        counts: dict[str, int] = {}
        for (text,) in self._conn.execute("SELECT text FROM segments"):
            for token in set(tokenize(str(text))):
                counts[token] = counts.get(token, 0) + 1
        self._conn.execute("DELETE FROM term_df")
        self._conn.executemany(
            "INSERT INTO term_df(term, doc_freq) VALUES (?, ?)",
            sorted(counts.items()),
        )
        self._conn.commit()
        return len(counts)

    def document_frequency(self, term: str) -> int:
        row = self._conn.execute(
            "SELECT doc_freq FROM term_df WHERE term=?", (str(term).casefold(),)
        ).fetchone()
        return int(row["doc_freq"]) if row else 0

    def commit(self) -> None:
        self._conn.commit()

    def counts(self) -> dict[str, int]:
        papers = int(self._conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        segments = int(self._conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0])
        return {"papers": papers, "segments": segments}

    # -- reading -----------------------------------------------------------

    def segments_for(self, paper_id: str, kind: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM segments WHERE paper_id=? AND segment_kind=? ORDER BY ordinal",
            (paper_id, kind),
        ))

    def paper(self, paper_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()

    def papers(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM papers ORDER BY paper_id"))


# --------------------------------------------------------------------------
# Material loading
# --------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_identity_map(path: Path) -> dict[str, dict[str, Any]]:
    """Load handle -> identity rows from the detailed outline."""

    payload = _read_json(Path(path))
    for key in ("source_identity_map", "source_index"):
        value = payload.get(key) if isinstance(payload, Mapping) else None
        if isinstance(value, Mapping) and value:
            return {str(handle): dict(row) for handle, row in value.items() if isinstance(row, Mapping)}
    raise PlanningMaterialSearchError("identity_map_not_found:" + str(path))


def _material_identity(card_path: Path) -> dict[str, Any]:
    try:
        card = _read_json(card_path)
    except (OSError, ValueError):
        return {}
    identity = card.get("paper_identity") if isinstance(card.get("paper_identity"), Mapping) else {}
    material = card.get("material") if isinstance(card.get("material"), Mapping) else {}
    return {"identity": dict(identity), "material": dict(material), "card": card}


def _usable_fields(material: Mapping[str, Any]) -> tuple[str, ...]:
    limits = material.get("usage_limits") if isinstance(material.get("usage_limits"), Mapping) else {}
    allowed = limits.get("allowed") or []
    return tuple(str(item) for item in allowed)


def _card_limits(material: Mapping[str, Any]) -> tuple[str, ...]:
    limits = material.get("usage_limits") if isinstance(material.get("usage_limits"), Mapping) else {}
    return tuple(str(item) for item in (limits.get("prohibited") or ()))


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _card_segments(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Turn the A/B planning card into readable, typed segments."""

    out: list[dict[str, Any]] = []
    planning = card.get("planning_view") if isinstance(card.get("planning_view"), Mapping) else {}
    review_plan = card.get("review_planning") if isinstance(card.get("review_planning"), Mapping) else {}
    general = card.get("general_understanding") if isinstance(card.get("general_understanding"), Mapping) else {}

    def add(kind: str, text: Any, section: str = "") -> None:
        body = "\n".join(_iter_strings(text)) if not isinstance(text, str) else text
        for chunk in split_reading_text(body):
            out.append({"segment_kind": kind, "text": chunk, "ordinal": len(out),
                        "section_path": (section,) if section else ()})

    add("card_planning_summary", planning.get("planning_summary") or review_plan.get("planning_summary"), "planning summary")
    add("card_work_summary", general.get("work_summary"), "general understanding / work summary")
    add("card_problem_question", general.get("problem_or_question"), "general understanding / problem")
    add("card_research_scope", general.get("research_scope"), "general understanding / scope")
    add("card_approach", general.get("approach"), "general understanding / approach")
    for row in general.get("key_findings") or ():
        if isinstance(row, Mapping):
            add("card_key_finding", row, "general understanding / key finding")
    for row in general.get("contribution_and_limits") or ():
        if isinstance(row, Mapping):
            add("card_contribution_limit", row, "general understanding / contribution and limits")
    for row in review_plan.get("facet_contributions") or ():
        if isinstance(row, Mapping):
            add("card_facet_contribution", row, "review planning / facet contribution")
    for row in review_plan.get("broader_review_uses") or ():
        if isinstance(row, Mapping):
            add("card_review_use", row, "review planning / broader review uses")
    for row in review_plan.get("scope_interpretation_cautions") or ():
        add("card_scope_caution", row, "review planning / scope cautions")
    for row in review_plan.get("topic_handles") or ():
        add("card_topic_handle", row, "review planning / topic handles")
    return out


def _snapshot_blocks(snapshot: Path) -> list[dict[str, Any]]:
    """Read DOCUMENT_BLOCKS.jsonl and split it into readable segments."""

    path = snapshot / "DOCUMENT_BLOCKS.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    ordinal = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("block_type") or "") in {"heading"}:
                continue
            text = str(row.get("text_normalized") or row.get("text_raw") or "").strip()
            if len(text) < 40:
                continue
            section = tuple(str(item) for item in (row.get("section_path") or ()))
            if _is_non_evidence_material(text, section):
                continue
            for chunk in split_reading_text(text):
                out.append({
                    "segment_kind": "document_block",
                    "text": chunk,
                    "ordinal": ordinal,
                    "section_path": section,
                    "char_start": int((row.get("locator") or {}).get("start") or 0),
                })
                ordinal += 1
    return out


def _reading_view_segments(snapshot: Path) -> list[dict[str, Any]]:
    """Fallback when a snapshot has only the flat reading view."""

    path = snapshot / "READING_VIEW.md"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    ordinal = 0
    for chunk in split_reading_text(path.read_text(encoding="utf-8", errors="replace")):
        if _is_non_evidence_material(chunk, ("reading view",)):
            continue
        out.append({"segment_kind": "document_block", "text": chunk, "ordinal": ordinal,
                    "section_path": ("reading view",)})
        ordinal += 1
    return out


def _directed_segments(path: Path) -> list[dict[str, Any]]:
    try:
        payload = _read_json(path)
    except (OSError, ValueError):
        return []
    artifact = payload.get("output") if isinstance(payload.get("output"), Mapping) else payload
    out: list[dict[str, Any]] = []
    questions = artifact.get("question_material") or (artifact.get("content") or {}).get("question_material") or []
    for row in questions:
        if not isinstance(row, Mapping):
            continue
        question = row.get("question") or row.get("gap_question") or ""
        if question:
            for chunk in split_reading_text(str(question)):
                out.append({"segment_kind": "directed_question", "text": chunk,
                            "ordinal": len(out), "section_path": ("directed reading / question",)})
        for key in ("answer", "material", "finding", "summary"):
            if row.get(key):
                for chunk in split_reading_text(str(row[key])):
                    out.append({"segment_kind": "directed_content", "text": chunk,
                                "ordinal": len(out), "section_path": ("directed reading / answer",)})
        for example in row.get("examples") or ():
            if isinstance(example, Mapping):
                blob = " ".join(
                    str(example.get(key) or "")
                    for key in ("finding", "conditions", "use_in_review", "attribution")
                )
                for chunk in split_reading_text(blob):
                    out.append({"segment_kind": "directed_content", "text": chunk,
                                "ordinal": len(out), "section_path": ("directed reading / example",)})
    for row in artifact.get("open_questions") or ():
        if isinstance(row, Mapping):
            blob = " ".join(str(item) for item in (row.get("remaining_points") or ()))
            for chunk in split_reading_text(blob):
                out.append({"segment_kind": "card_open_question", "text": chunk,
                            "ordinal": len(out), "section_path": ("directed reading / open questions",)})
    return out


def _find_snapshot(paper_id: str, roots: Sequence[Path]) -> Path | None:
    for root in roots:
        if not root.is_dir():
            continue
        for snapshot in sorted(root.glob(paper_id + "/snapshot-*")):
            if snapshot.is_dir():
                return snapshot
    return None


def _find_directed(paper_id: str, roots: Sequence[Path]) -> Path | None:
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / paper_id / "DIRECTED_READING.json"
        if direct.is_file():
            return direct
        for candidate in root.glob("*/" + paper_id + "/DIRECTED_READING.json"):
            if candidate.is_file():
                return candidate
    return None


def _fallback_card(config: SearchIndexConfig, paper_id: str) -> Path | None:
    for root in config.synthetic_card_roots:
        candidate = Path(root) / paper_id / "PAPER_READING_CARD.json"
        if candidate.is_file():
            return candidate
    return None


def _load_card_indexed_materials(config: SearchIndexConfig) -> tuple[dict[str, dict[str, Any]], str]:
    """Read the topic pool and identity map without opening any snapshot."""

    pool = [json.loads(line) for line in Path(config.pool_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id: dict[str, dict[str, Any]] = {}
    for row in pool:
        pid = str(row.get("paper_id") or "").strip()
        if pid:
            by_id[pid] = dict(row)
    return by_id, "pool"


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


@dataclass
class BuildReport:
    topic_id: str
    index_path: str
    pool_rows: int
    identity_handles: int
    papers_written: int
    papers_new: int
    segments_written: int
    papers_with_document_blocks: int
    papers_with_card_only: int
    missing_material: list[dict[str, str]]
    index_counts: dict[str, int]
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "index_path": self.index_path,
            "pool_rows": self.pool_rows,
            "identity_handles": self.identity_handles,
            "papers_written": self.papers_written,
            "papers_new": self.papers_new,
            "segments_written": self.segments_written,
            "papers_with_document_blocks": self.papers_with_document_blocks,
            "papers_with_card_only": self.papers_with_card_only,
            "missing_material": self.missing_material,
            "index_counts": self.index_counts,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


def build_index(
    config: SearchIndexConfig,
    *,
    index: PlanningMaterialIndex | None = None,
    report_path: Path | None = None,
    verbose: bool = False,
) -> BuildReport:
    """Build or extend the topic index from registered local material."""

    import time

    started = time.time()
    own_index = index is None
    index = index or PlanningMaterialIndex(config.index_path)
    try:
        pool_by_id, _ = _load_card_indexed_materials(config)
        identity = load_identity_map(Path(config.identity_map_path))
        # Every pool row that also appears in the identity map is indexed under
        # its handle; extra handles (external supplements) are indexed too.
        handle_by_paper: dict[str, str] = {}
        for handle, row in identity.items():
            pid = str(row.get("paper_id") or "").strip()
            if pid and pid not in handle_by_paper:
                handle_by_paper[pid] = handle
        ordered_papers: list[str] = []
        for pid in pool_by_id:
            if pid not in ordered_papers:
                ordered_papers.append(pid)
        for handle in sorted(identity):
            pid = str(identity[handle].get("paper_id") or "").strip()
            if pid and pid not in ordered_papers:
                ordered_papers.append(pid)

        papers_new = 0
        segments_written = 0
        with_blocks = 0
        card_only = 0
        missing: list[dict[str, str]] = []
        processed = 0
        for pid in ordered_papers:
            handle = handle_by_paper.get(pid, "")
            row = identity.get(handle) or {}
            pool_row = pool_by_id.get(pid) or {}
            planning = pool_row.get("planning_view") if isinstance(pool_row.get("planning_view"), Mapping) else {}
            pool_identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
            card_path = Path(str(row.get("card_path") or pool_row.get("card_path") or ""))
            title = str(row.get("title") or pool_identity.get("title") or "")
            year = str(row.get("year") or pool_identity.get("year") or "")
            doi = str(row.get("doi") or pool_identity.get("doi") or "")
            material_depth = str(planning.get("declared_content_depth") or "")
            card_payload: dict[str, Any] = {}
            if card_path.is_file():
                card_payload = _material_identity(card_path)
                material = card_payload.get("material") or {}
                material_depth = str(material.get("declared_content_depth") or material_depth)
                identity_block = card_payload.get("identity") or {}
                year = str(year or identity_block.get("year") or "")
                doi = str(doi or identity_block.get("doi") or "")
            if not card_path.is_file():
                fallback = _fallback_card(config, pid)
                if fallback is not None:
                    card_path = fallback
                    card_payload = _material_identity(card_path)
            snapshot = _find_snapshot(pid, config.snapshot_roots)
            directed = _find_directed(pid, config.directed_roots)
            record = PaperRecord(
                paper_id=pid,
                source_handle=handle,
                title=title,
                year=year,
                doi=doi,
                card_path=str(card_path) if card_path.is_file() or str(card_path) else "",
                snapshot_path=str(snapshot) if snapshot else "",
                material_depth=material_depth,
                identity_status="pool_identity" if handle in identity else "pool_row_only",
                pool_action="already_in_pool",
                directed_reading_path=str(directed) if directed else "",
            )
            if index.upsert_paper(record):
                papers_new += 1
            card = card_payload.get("card") if isinstance(card_payload.get("card"), Mapping) else {}
            paper_segments: list[dict[str, Any]] = []
            if card:
                card_segments = _card_segments(card)
                paper_segments.extend(card_segments)
                index.set_paper_terms(pid, [
                    str(item) for item in (card.get("review_planning") or {}).get("topic_handles") or ()
                ])
            if snapshot is not None:
                blocks = _snapshot_blocks(snapshot) or _reading_view_segments(snapshot)
                if blocks:
                    with_blocks += 1
                paper_segments.extend(blocks)
            else:
                card_only += 1
                if not card:
                    missing.append({"paper_id": pid, "source_handle": handle, "title": title,
                                    "reason": "no_snapshot_and_unreadable_card"})
            if directed is not None:
                paper_segments.extend(_directed_segments(directed))
            # One batched replace per paper: no kind can survive from an older
            # build, and one paper can never appear as several candidates.
            segments_written += index.bulk_replace_paper(pid, paper_segments)
            processed += 1
            if processed % 25 == 0:
                index.commit()
        index.commit()
        index.record_term_document_frequency()
        report = BuildReport(
            topic_id=config.topic_id,
            index_path=str(config.index_path),
            pool_rows=len(pool_by_id),
            identity_handles=len(identity),
            papers_written=len(ordered_papers),
            papers_new=papers_new,
            segments_written=segments_written,
            papers_with_document_blocks=with_blocks,
            papers_with_card_only=card_only,
            missing_material=missing,
            index_counts=index.counts(),
            elapsed_seconds=time.time() - started,
        )
        if report_path is not None:
            Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        if own_index:
            index.close()


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def _coverage(text_tokens: set[str], terms: Sequence[QueryTerm]) -> tuple[float, tuple[str, ...]]:
    matched: list[str] = []
    for term in terms:
        forms = set()
        for form in term.forms:
            forms.update(tokenize(form))
        if forms and forms & text_tokens:
            matched.append(term.concept)
    return (len(matched) / len(terms) if terms else 0.0), tuple(matched)


def _term_forms(term: QueryTerm) -> set[str]:
    forms: set[str] = set()
    for form in term.forms:
        forms.update(tokenize(form))
    return forms


def _dedupe_terms(terms: Sequence[QueryTerm]) -> list[QueryTerm]:
    seen: set[str] = set()
    ordered: list[QueryTerm] = []
    for term in terms:
        if term.concept in seen:
            continue
        seen.add(term.concept)
        ordered.append(term)
    return ordered


class _IdfWeights:
    """Inverse-document-frequency weights over the indexed segments."""

    def __init__(self, index: "PlanningMaterialIndex") -> None:
        self._index = index
        self._documents = max(1, index.counts()["segments"])
        self._cache: dict[str, float] = {}

    def weight(self, token: str) -> float:
        if token not in self._cache:
            df = self._index.document_frequency(token)
            self._cache[token] = math.log((self._documents + 1.0) / (df + 1.0)) + 1.0
        return self._cache[token]

    def concept_score(self, counts: Mapping[str, int], forms: set[str]) -> float:
        if not forms:
            return 0.0
        present = [count for token, count in counts.items() if token in forms and count]
        if not present:
            return 0.0
        distinct = len(present)
        total = sum(present)
        mean_weight = sum(
            self.weight(token) for token in counts if token in forms and counts[token]
        ) / distinct
        return mean_weight * total / math.sqrt(total)


def _adjacent_terms(text: str, forms: set[str], pairs: set[tuple[str, str]]) -> bool:
    """True when two different concept words appear back to back.

    "fungal species", "butyrate concentration" and "tumour types" are the
    question's own phrasing; a passage that writes them next to each other is
    about the concept, while the same words scattered across a long review are
    not.
    """

    text = str(text or "")
    if not text or not pairs:
        return False
    previous: str | None = None
    for match in re.finditer(r"[A-Za-z0-9\-]+", text):
        token = match.group(0).casefold()
        if token in forms:
            if previous is not None and (previous, token) in pairs:
                return True
            previous = token
        elif len(token) > 2:
            previous = None
    return False


def search(
    index: PlanningMaterialIndex,
    question: str,
    *,
    concepts: Sequence[str] = (),
    top_papers: int = 8,
    passages_per_paper: int = 2,
    context_chars: int = 160,
    max_candidates: int = BM25_CANDIDATES,
    kinds: Sequence[str] | None = None,
    required_concepts: Sequence[str] = (),
    paper_ids: Sequence[str] = (),
) -> SearchResult:
    """Answer one concrete question from local material only.

    ``concepts`` lets the caller send the question the way the planner actually
    forms it: a natural-language sentence plus the explicit concepts that must be
    present (a metabolite, a receptor, a compartment, a study design).  Prose
    alone drifts, because expansion turns a long sentence into dozens of weak OR
    terms.

    ``required_concepts`` turns the question's own hard requirements into a
    filter: a passage that only overlaps on generic wording is not allowed to
    stand in for an answer about a compartment, an assay, or a population that
    was never measured.
    """

    prose_terms = expand_query(question)
    explicit_terms = [concept_term(concept) for concept in concepts]
    terms = _dedupe_terms([*explicit_terms, *prose_terms])
    extras = [concept_term(concept) for concept in required_concepts]
    # Every question word becomes an OR-group, so a long question would pull tens
    # of thousands of generic passages into the candidate pool and dilute recall.
    # Explicit concepts come first, then the leading prose concepts; the full
    # concept list still drives the local ranking.
    match = fts_query(_dedupe_terms([*terms[:MAX_FTS_CONCEPTS], *extras]))
    index_counts = index.counts()
    # Restrict before the global BM25 cap so explicitly named local papers
    # get the same scoring path even when absent from the ordinary window.
    nominated = tuple(dict.fromkeys(str(item) for item in paper_ids if str(item)))
    restriction = " AND segment_fts.paper_id IN (" + ",".join("?" for _ in nominated) + ")" if nominated else ""
    rows = list(index._conn.execute(
        f"""
        SELECT segment_fts.segment_id AS segment_id,
               segment_fts.paper_id AS paper_id,
               segment_fts.segment_kind AS segment_kind,
               bm25(segment_fts) AS rank
        FROM segment_fts
        WHERE segment_fts MATCH ? {restriction}
        ORDER BY rank
        LIMIT ?
        """,
        (match, *nominated, max(1, int(max_candidates))),
    ))
    result = SearchResult(
        question=question,
        query_terms=tuple(terms),
        index_papers=index_counts["papers"],
        index_segments=index_counts["segments"],
    )
    if not rows:
        result.not_matched_reason = "no_local_passage_matched_the_query_terms"
        return result

    weights = _IdfWeights(index)
    prepared: list[tuple[QueryTerm, set[str]]] = [(term, _term_forms(term)) for term in terms]
    extra_forms = [(extra, _term_forms(extra)) for extra in extras]
    pairs = _query_pairs(terms)
    allowed = {str(k) for k in kinds} if kinds else None

    segment_rows: dict[str, sqlite3.Row] = {}
    for chunk_start in range(0, len(rows), 400):
        chunk = [str(row["segment_id"]) for row in rows[chunk_start:chunk_start + 400]]
        placeholders = ",".join("?" * len(chunk))
        for segment in index._conn.execute(
            f"SELECT * FROM segments WHERE segment_id IN ({placeholders})", chunk
        ):
            segment_rows[str(segment["segment_id"])] = segment

    scored_papers: dict[str, dict[str, Any]] = {}
    for row in rows:
        segment = segment_rows.get(str(row["segment_id"]))
        if segment is None:
            continue
        # Apply the content boundary at query time as well as build time.  The
        # shipped topic index may predate this filter, and rebuilding it is not
        # required for callers to stop seeing references or declarations.
        section_path = tuple(
            part for part in str(segment["section_path"]).split(" / ") if part
        )
        if _is_non_evidence_material(str(segment["text"]), section_path):
            continue
        if allowed is not None and str(segment["segment_kind"]) not in allowed:
            continue
        tokens = tokenize(str(segment["text"]))
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        distinct = set(counts)
        if extras and not all(forms & distinct for _extra, forms in extra_forms):
            continue
        matched: list[str] = []
        combined: set[str] = set()
        score = 0.0
        for term, forms in prepared:
            value = weights.concept_score(counts, forms)
            if value:
                matched.append(term.concept)
                score += value
                combined |= forms
        if not matched:
            continue
        # Prefer passages where the question's own words sit next to each other:
        # "fungal species" or "butyrate concentration" is about the concept,
        # while the same words scattered across a long review are not.
        if _adjacent_terms(str(segment["text"]), combined, pairs):
            score *= PROXIMITY_BONUS
        score *= KIND_WEIGHTS.get(str(segment["segment_kind"]), 0.5)
        score *= _unlabelled_reference_likelihood(str(segment["text"]), section_path)
        coverage = len(matched) / len(prepared) if prepared else 0.0
        # Reward breadth as well as depth, but never enough to let a long
        # all-purpose review outrank a specific, on-point passage.
        score *= SEGMENT_COVERAGE_FLOOR + (1.0 - SEGMENT_COVERAGE_FLOOR) * coverage
        entry = scored_papers.setdefault(str(row["paper_id"]), {"passages": [], "coverage": 0.0})
        entry["passages"].append((score, coverage, tuple(matched), segment))
        entry["coverage"] = max(entry["coverage"], coverage)

    result.candidate_papers = len(scored_papers)
    if not scored_papers:
        result.not_matched_reason = (
            "no_local_passage_satisfied_the_required_concepts"
            if extras else "matched_segments_filtered_out_by_kind"
        )
        return result

    ordered = sorted(
        scored_papers.items(),
        key=lambda item: (
            -_paper_score(item[1]["passages"], concepts=len(prepared)),
            -item[1]["coverage"],
            item[0],
        ),
    )

    for paper_id, entry in ordered[: max(0, int(top_papers))]:
        paper = index.paper(paper_id)
        if paper is None:
            continue
        passages = sorted(entry["passages"], key=lambda item: (-item[0], -item[1]))
        chosen: list[Any] = []
        used_kinds: set[str] = set()
        for passage in passages:
            kind = str(passage[3]["segment_kind"])
            if kind in used_kinds and len(chosen) >= 1:
                continue
            chosen.append(passage)
            used_kinds.add(kind)
            if len(chosen) >= max(1, int(passages_per_paper)):
                break
        for score, coverage, matched_terms, segment in chosen:
            text = _with_context(index, segment, context_chars=context_chars)
            result.hits.append(SearchHit(
                paper_id=paper_id,
                source_handle=str(paper["source_handle"]),
                title=str(paper["title"]),
                year=str(paper["year"]),
                doi=str(paper["doi"]),
                segment_kind=str(segment["segment_kind"]),
                section_path=tuple(
                    part for part in str(segment["section_path"]).split(" / ") if part
                ),
                text=text,
                score=score,
                match_terms=matched_terms,
                material_depth=str(paper["material_depth"]),
                reading_path=str(paper["snapshot_path"]),
                card_path=str(paper["card_path"]),
                pool_action=str(paper["pool_action"]),
                best_sentence=_best_sentence(str(segment["text"]), prepared, weights, pairs),
            ))
    result.matched_papers = len({hit.paper_id for hit in result.hits})
    if not result.hits:
        result.not_matched_reason = "no_paper_passed_ranking"
    return result


def nominated_paper_passage(index: PlanningMaterialIndex, paper_id: str) -> SearchHit | None:
    """One substantive existing summary/opening for an explicit nomination.

    This is an identity-directed reading opportunity when query vocabulary does
    not match, not a search relevance score. It does not inspect external files
    or substitute references/declarations for readable material.
    """
    paper = index.paper(paper_id)
    if paper is None:
        return None
    for kind in ("card_work_summary", "card_planning_summary", "card_key_finding", "document_block"):
        for segment in index.segments_for(paper_id, kind):
            body = str(segment["text"]).strip()
            section = tuple(part for part in str(segment["section_path"]).split(" / ") if part)
            if not body or _is_non_evidence_material(body, section):
                continue
            return SearchHit(
                paper_id=paper_id, source_handle=str(paper["source_handle"]),
                title=str(paper["title"]), year=str(paper["year"]), doi=str(paper["doi"]),
                segment_kind=kind, section_path=section, text=body, score=0.0,
                match_terms=(), material_depth=str(paper["material_depth"]),
                reading_path=str(paper["snapshot_path"]), card_path=str(paper["card_path"]),
                pool_action=str(paper["pool_action"]), best_sentence="",
            )
    return None


def _query_pairs(terms: Sequence[QueryTerm]) -> set[tuple[str, str]]:
    """Ordered token pairs that appear inside the question's own concepts."""

    pairs: set[tuple[str, str]] = set()
    for term in terms:
        for form in term.forms:
            tokens = tokenize(form)
            for left, right in zip(tokens, tokens[1:]):
                if left != right:
                    pairs.add((left, right))
    return pairs


def _paper_score(passages: Sequence[Any], *, concepts: int) -> float:
    """Rank a paper by its best passages and by how much of the question it covers.

    A paper that treats the question across several passages should beat a paper
    that happens to repeat two query words once, so the top passage scores are
    combined with diminishing weight and scaled by concept coverage.
    """

    scores = sorted((float(passage[0]) for passage in passages), reverse=True)[:PAPER_SCORE_PASSAGES]
    if not scores:
        return 0.0
    weights = (1.0, 0.35, 0.15)
    combined = sum(score * weights[position] for position, score in enumerate(scores))
    distinct: set[str] = set()
    for passage in passages:
        distinct.update(passage[2])
    coverage = len(distinct) / concepts if concepts else 1.0
    return combined * (PAPER_COVERAGE_FLOOR + (1.0 - PAPER_COVERAGE_FLOOR) * coverage)


def _best_sentence(
    text: str,
    prepared: Sequence[tuple[QueryTerm, set[str]]],
    weights: "_IdfWeights",
    pairs: set[tuple[str, str]],
) -> str:
    """The sentence inside a returned segment that carries the most signal."""

    if not prepared:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", str(text)) if part.strip()]
    if not sentences:
        return ""
    best = ""
    best_score = 0.0
    for sentence in sentences:
        counts: dict[str, int] = {}
        for token in tokenize(sentence):
            counts[token] = counts.get(token, 0) + 1
        score = 0.0
        combined: set[str] = set()
        for _term, forms in prepared:
            value = weights.concept_score(counts, forms)
            if value:
                score += value
                combined |= forms
        if not score:
            continue
        if _adjacent_terms(sentence, combined, pairs):
            score *= PROXIMITY_BONUS
        # A sentence that answers the question is usually a claim, and claims in
        # this material carry a number or a named entity rather than a citation.
        if re.search(r"\d", sentence):
            score *= 1.15
        score /= 1.0 + len(sentence) / 1500.0
        if score > best_score:
            best_score = score
            best = sentence
    return best if best_score else sentences[0][:400]


def _with_context(index: PlanningMaterialIndex, segment: sqlite3.Row, *, context_chars: int) -> str:
    """Attach neighbouring sentences so the passage is readable on its own."""

    body = str(segment["text"])
    if context_chars <= 0:
        return body
    siblings = index.segments_for(str(segment["paper_id"]), str(segment["segment_kind"]))
    ordinals = [int(row["ordinal"]) for row in siblings]
    position = ordinals.index(int(segment["ordinal"])) if int(segment["ordinal"]) in ordinals else -1
    before = ""
    after = ""
    if position > 0:
        previous = siblings[position - 1]
        previous_path = tuple(part for part in str(previous["section_path"]).split(" / ") if part)
        if not _is_non_evidence_material(str(previous["text"]), previous_path):
            before = str(previous["text"])[-context_chars:]
    if 0 <= position < len(siblings) - 1:
        following = siblings[position + 1]
        following_path = tuple(part for part in str(following["section_path"]).split(" / ") if part)
        if not _is_non_evidence_material(str(following["text"]), following_path):
            after = str(following["text"])[:context_chars]
    return (before + " " + body + " " + after).strip()


@dataclass
class PaperContext:
    """One paper's own opening and section map, for a bounded question-shaped read.

    Work order 06 measured that reading only the matched segments hides a fact the
    same paper states elsewhere: the fungal meta-analysis names its species in
    the abstract, while the matched passage was a later results figure caption.
    A bounded read of one paper therefore also carries its opening and its
    section list, which is what makes "read this paper for this question"
    answerable without a whole-paper re-read.
    """

    paper_id: str
    source_handle: str
    title: str
    year: str
    doi: str
    reading_path: str
    card_path: str
    opening: str
    sections: tuple[str, ...]
    matched_sections: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "source_handle": self.source_handle,
            "title": self.title,
            "year": self.year,
            "doi": self.doi,
            "reading_path": self.reading_path,
            "card_path": self.card_path,
            "opening": self.opening,
            "sections": list(self.sections),
            "matched_sections": list(self.matched_sections),
        }


def paper_context(
    index: PlanningMaterialIndex,
    paper_ids: Sequence[str],
    *,
    opening_chars: int = 1400,
    max_sections: int = 18,
) -> list[PaperContext]:
    """Opening text and section map for the papers a read is focused on."""

    out: list[PaperContext] = []
    for paper_id in paper_ids:
        paper = index.paper(paper_id)
        if paper is None:
            continue
        segments = list(index._conn.execute(
            "SELECT segment_kind, ordinal, section_path, text FROM segments "
            "WHERE paper_id=? ORDER BY segment_kind, ordinal",
            (paper_id,),
        ))
        opening_parts: list[str] = []
        sections: list[str] = []
        matched_sections: list[str] = []
        for row in segments:
            section = str(row["section_path"] or "")
            if section and section not in sections:
                sections.append(section)
            if str(row["segment_kind"]) == "document_block" and section:
                if section not in matched_sections:
                    matched_sections.append(section)
            if str(row["segment_kind"]) == "document_block" and len(" ".join(opening_parts)) < opening_chars:
                opening_parts.append(str(row["text"]))
        out.append(PaperContext(
            paper_id=paper_id,
            source_handle=str(paper["source_handle"]),
            title=str(paper["title"]),
            year=str(paper["year"]),
            doi=str(paper["doi"]),
            reading_path=str(paper["snapshot_path"]),
            card_path=str(paper["card_path"]),
            opening=" ".join(opening_parts)[:opening_chars],
            sections=tuple(sections[:max_sections]),
            matched_sections=tuple(matched_sections[:max_sections]),
        ))
    return out
