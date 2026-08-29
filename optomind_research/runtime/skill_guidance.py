"""Deterministic provenance/metadata contract for OptoMind command-knowledge skills.

OptoMind's scientific-writing skills are dense command knowledge: planning,
authoring, and audit instructions consumed by Qwen and validated by local code.
They are never scientific evidence.  This module gives downstream code a
deterministic way to:

- discover command-knowledge skills (SKILL.md plus provenance.json);
- validate machine-readable provenance metadata;
- enforce the evidence-prohibition boundary (no citations or concrete REF
  markers inside instruction bodies);
- build a compact prompt block containing instructions only, with raw
  provenance metadata and influence/license details excluded.

The existing AgentScope LocalSkillLoader API is preserved; this contract is a
complementary layer, not a replacement.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import frontmatter

logger = logging.getLogger(__name__)

SKILL_METADATA_FILENAME = "provenance.json"
SKILL_MANUAL_FILENAME = "SKILL.md"
SCHEMA_VERSION = "1.0"

REQUIRED_METADATA_FIELDS = (
    "schema_version",
    "skill",
    "skill_version",
    "role",
    "adoption_mode",
    "applicability",
    "evidence_prohibition",
)

SOURCE_REQUIRED_FIELDS = (
    "project",
    "repository",
    "commit",
    "stars_observed",
    "stars_observed_date",
)

INFLUENCE_REQUIRED_FIELDS = (
    "project",
    "repository",
    "commit",
    "stars_observed",
    "stars_observed_date",
    "license",
    "adoption_mode",
    "notes",
)

# star counts are genuinely "not recorded" in some provenance files
_ALLOW_EMPTY_SOURCE_FIELDS = frozenset({"stars_observed", "stars_observed_date"})
_ALLOW_EMPTY_INFLUENCE_FIELDS = frozenset({"stars_observed", "stars_observed_date"})

ALLOWED_ADOPTION_MODES = frozenset({"original_synthesis", "compatible_adaptation"})
ALLOWED_INFLUENCE_ADOPTION_MODES = frozenset({"idea_only", "compatible_adaptation"})

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# REF marker tokens that are instructional placeholders rather than citations.
_REF_PLACEHOLDERS = frozenset(
    {
        "*",
        "UNKNOWN",
        "unknown",
        "paper_id",
        "PAPER_ID",
        "paper_ids",
        "chunk_id",
        "chunk_ids",
        "id",
        "ids",
        "xxx",
        "REPLACE_WITH_PAPER_ID",
    }
)

_CITATION_RE = re.compile(
    r"https?://|www\.|doi\.org/|arxiv\.org/abs/|"
    r"\b10\.\d{4,9}/[^\s)]+|"
    r"\barXiv:\s*\d{4}\.\d{4,5}\b"
)
_REF_MARKER_RE = re.compile(r"\[REF:([^\]]+)\]")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class SkillMetadataError(ValueError):
    """Raised when a skill violates the command-knowledge provenance contract."""


@dataclass(frozen=True)
class SkillProvenance:
    """Validated machine-readable provenance for one command-knowledge skill."""

    skill: str
    skill_version: str
    schema_version: str
    role: str
    adoption_mode: str
    applicability: str
    evidence_prohibition: bool
    evidence_prohibition_text: str
    source: Dict[str, Any]
    influences: List[Dict[str, Any]]
    raw: Dict[str, Any]


@dataclass(frozen=True)
class CommandKnowledgeBundle:
    """Compact command-knowledge payload suitable for prompt injection.

    The prompt block intentionally contains instructions only.  Raw provenance
    metadata (source commit, licenses, influence details) is available through
    ``provenance`` for deterministic code but is excluded from the prompt text.
    """

    name: str
    version: str
    role: str
    instructions: str
    evidence_prohibition: bool
    applicability: str
    provenance: SkillProvenance

    @property
    def status_line(self) -> str:
        return (
            "command knowledge only - NOT scientific evidence; "
            "contains no citations or paper facts"
        )

    def to_prompt_block(self) -> str:
        parts = [
            f"[SKILL:{self.name} v{self.version}]",
            f"STATUS: {self.status_line}",
            f"ROLE: {self.role}",
            self.instructions.strip(),
        ]
        return "\n\n".join(parts)


def _default_skills_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


def _require_fields(
    raw: Dict[str, Any],
    fields: Iterable[str],
    where: str,
    *,
    allow_empty: frozenset[str] = frozenset(),
) -> None:
    missing = [field for field in fields if field not in raw]
    if missing:
        raise SkillMetadataError(
            f"{where} missing required field(s): {', '.join(missing)}"
        )
    empty = [
        field
        for field in fields
        if field not in allow_empty and raw[field] in (None, "")
    ]
    if empty:
        raise SkillMetadataError(
            f"{where} has empty required field(s): {', '.join(empty)}"
        )


def validate_provenance_metadata(raw: Dict[str, Any]) -> None:
    """Validate the machine-readable command-knowledge metadata contract."""

    if not isinstance(raw, dict):
        raise SkillMetadataError("provenance.json must contain a JSON object")
    _require_fields(raw, REQUIRED_METADATA_FIELDS, "provenance.json")
    if str(raw["schema_version"]) != SCHEMA_VERSION:
        raise SkillMetadataError(
            f"unsupported schema_version: {raw['schema_version']!r}; "
            f"expected {SCHEMA_VERSION}"
        )
    if not VERSION_RE.match(str(raw["skill_version"])):
        raise SkillMetadataError(
            f"invalid skill_version: {raw['skill_version']!r}; "
            "expected MAJOR.MINOR.PATCH"
        )
    if str(raw["adoption_mode"]) not in ALLOWED_ADOPTION_MODES:
        raise SkillMetadataError(
            f"invalid adoption_mode: {raw['adoption_mode']!r}"
        )
    if raw["evidence_prohibition"] is not True:
        raise SkillMetadataError(
            "evidence_prohibition must be true for command-knowledge skills"
        )

    source = raw.get("source")
    if not isinstance(source, dict):
        raise SkillMetadataError("source must be an object")
    _require_fields(
        source,
        SOURCE_REQUIRED_FIELDS,
        "source",
        allow_empty=_ALLOW_EMPTY_SOURCE_FIELDS,
    )

    influences = raw.get("influences")
    if not isinstance(influences, list) or not influences:
        raise SkillMetadataError("influences must be a non-empty list")
    for index, influence in enumerate(influences):
        if not isinstance(influence, dict):
            raise SkillMetadataError(f"influences[{index}] must be an object")
        _require_fields(
            influence,
            INFLUENCE_REQUIRED_FIELDS,
            f"influences[{index}]",
            allow_empty=_ALLOW_EMPTY_INFLUENCE_FIELDS,
        )
        if (
            str(influence["adoption_mode"])
            not in ALLOWED_INFLUENCE_ADOPTION_MODES
        ):
            raise SkillMetadataError(
                f"influences[{index}] invalid adoption_mode: "
                f"{influence['adoption_mode']!r}"
            )


def load_provenance(skill_dir: Path) -> SkillProvenance:
    """Load and validate provenance.json for one skill directory."""

    skill_dir = Path(skill_dir)
    metadata_path = skill_dir / SKILL_METADATA_FILENAME
    if not metadata_path.is_file():
        raise SkillMetadataError(
            f"missing {SKILL_METADATA_FILENAME} in {skill_dir}"
        )
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillMetadataError(
            f"invalid JSON in {metadata_path}: {exc}"
        ) from exc
    validate_provenance_metadata(raw)
    return SkillProvenance(
        skill=str(raw["skill"]),
        skill_version=str(raw["skill_version"]),
        schema_version=str(raw["schema_version"]),
        role=str(raw["role"]),
        adoption_mode=str(raw["adoption_mode"]),
        applicability=str(raw["applicability"]),
        evidence_prohibition=bool(raw["evidence_prohibition"]),
        evidence_prohibition_text=str(
            raw.get("evidence_prohibition_text")
            or "This skill is command knowledge, not scientific evidence."
        ),
        source=dict(raw.get("source") or {}),
        influences=[dict(item) for item in raw.get("influences") or []],
        raw=dict(raw),
    )


def _extract_frontmatter_and_body(text: str) -> tuple[Dict[str, Any], str]:
    try:
        post = frontmatter.loads(text)
        return dict(post.metadata), str(post.content).strip()
    except Exception as exc:
        raise SkillMetadataError(
            f"SKILL.md frontmatter is invalid: {exc}"
        ) from exc


def load_instructions(skill_dir: Path) -> str:
    """Return the SKILL.md instruction body without frontmatter."""

    skill_dir = Path(skill_dir)
    manual_path = skill_dir / SKILL_MANUAL_FILENAME
    if not manual_path.is_file():
        raise SkillMetadataError(f"missing {SKILL_MANUAL_FILENAME} in {skill_dir}")
    _, body = _extract_frontmatter_and_body(
        manual_path.read_text(encoding="utf-8")
    )
    return body


def _citation_violation(instructions: str) -> Optional[str]:
    """Return a description of the first citation/fact marker found, or None."""

    match = _CITATION_RE.search(instructions)
    if match:
        return (
            "instruction body contains citation/fact marker: "
            f"{match.group(0)[:60]!r}"
        )
    for match in _REF_MARKER_RE.finditer(instructions):
        token = match.group(1).strip()
        if token not in _REF_PLACEHOLDERS:
            return (
                "instruction body contains concrete REF marker: "
                f"[REF:{token}]"
            )
    return None


def validate_evidence_prohibition(
    provenance: SkillProvenance,
    instructions: str,
) -> None:
    """Enforce the evidence-prohibition boundary deterministically."""

    if not provenance.evidence_prohibition:
        raise SkillMetadataError("evidence_prohibition must be true")
    if len(instructions) < 200:
        raise SkillMetadataError(
            "instruction body is too short to be a dense command manual"
        )
    violation = _citation_violation(instructions)
    if violation is not None:
        raise SkillMetadataError(violation)


def load_command_bundle(skill_dir: Path) -> CommandKnowledgeBundle:
    """Load and validate one command-knowledge skill into a prompt-ready bundle."""

    skill_dir = Path(skill_dir)
    provenance = load_provenance(skill_dir)
    instructions = load_instructions(skill_dir)
    validate_evidence_prohibition(provenance, instructions)
    return CommandKnowledgeBundle(
        name=provenance.skill,
        version=provenance.skill_version,
        role=provenance.role,
        instructions=instructions,
        evidence_prohibition=provenance.evidence_prohibition,
        applicability=provenance.applicability,
        provenance=provenance,
    )


def discover_skill_dirs(skills_dir: Path) -> List[Path]:
    """Return every directory containing SKILL.md (AgentScope parity)."""

    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        raise SkillMetadataError(f"skills directory not found: {skills_dir}")
    return sorted(manual.parent for manual in skills_dir.rglob(SKILL_MANUAL_FILENAME))


def _is_command_skill_dir(skill_dir: Path) -> bool:
    return (skill_dir / SKILL_MANUAL_FILENAME).is_file() and (
        skill_dir / SKILL_METADATA_FILENAME
    ).is_file()


def discover_command_bundles(
    skills_dir: Optional[Path] = None,
    names: Optional[Iterable[str]] = None,
    *,
    strict: bool = True,
) -> List[CommandKnowledgeBundle]:
    """Discover validated command-knowledge skills.

    Directories without provenance.json are ordinary AgentScope skills and are
    skipped (they are outside this contract).  Directories that declare
    provenance.json but fail validation raise ``SkillMetadataError`` when
    ``strict`` is True, or are skipped with a warning when it is False.
    """

    root = Path(skills_dir) if skills_dir is not None else _default_skills_dir()
    wanted = {str(name) for name in names} if names is not None else None
    bundles: List[CommandKnowledgeBundle] = []
    for skill_dir in discover_skill_dirs(root):
        if not _is_command_skill_dir(skill_dir):
            continue
        try:
            bundle = load_command_bundle(skill_dir)
        except SkillMetadataError as exc:
            if strict:
                raise
            logger.warning(
                "Skipping invalid command-knowledge skill %s: %s",
                skill_dir,
                exc,
            )
            continue
        if wanted is not None and bundle.name not in wanted:
            continue
        bundles.append(bundle)
    return bundles


def load_command_bundle_by_name(
    name: str,
    skills_dir: Optional[Path] = None,
) -> CommandKnowledgeBundle:
    """Load one command-knowledge skill by its metadata ``skill`` name."""

    root = Path(skills_dir) if skills_dir is not None else _default_skills_dir()
    for skill_dir in discover_skill_dirs(root):
        if not _is_command_skill_dir(skill_dir):
            continue
        try:
            provenance = load_provenance(skill_dir)
        except SkillMetadataError:
            continue
        if provenance.skill == name:
            return load_command_bundle(skill_dir)
    raise SkillMetadataError(f"command-knowledge skill not found: {name}")


def build_skill_guidance_prompt(
    skills_dir: Optional[Path] = None,
    skill_names: Optional[Iterable[str]] = None,
) -> str:
    """Build a compact prompt-injection block from command-knowledge skills.

    The returned text contains instruction blocks only; provenance metadata and
    influence/license details are excluded from the prompt block.
    """

    bundles = discover_command_bundles(skills_dir, names=skill_names)
    return "\n\n".join(bundle.to_prompt_block() for bundle in bundles)
