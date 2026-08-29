"""Skill loader wrapper — provides LocalSkillLoader for ResearchWorker."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Optional

from agentscope.skill import LocalSkillLoader, SkillLoaderBase

from .skill_guidance import (
    CommandKnowledgeBundle,
    build_skill_guidance_prompt,
    discover_command_bundles,
    load_command_bundle_by_name,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILLS_DIR = PROJECT_ROOT / "skills"


def get_skill_loader(skills_dir: Path | None = None, scan_subdir: bool = True) -> LocalSkillLoader:
    """Return a LocalSkillLoader pointed at the project skills directory."""
    target = skills_dir or DEFAULT_SKILLS_DIR
    return LocalSkillLoader(directory=str(target), scan_subdir=scan_subdir)


class FilteredSkillLoader(SkillLoaderBase):
    """Wraps a SkillLoaderBase and exposes only the specified skill_ids.

    Semantics:
    - skill_ids=None  → expose all skills (internal/backward-compat use only)
    - skill_ids=[]    → expose NO skills (default from TaskContract)
    - skill_ids=["*"] → expose all skills (explicit wildcard)
    - skill_ids=[...] → expose only named skills

    This is a thin filter wrapper — the underlying loader still scans the
    directory; we just filter what gets returned to the Toolkit.
    """

    def __init__(
        self,
        inner: SkillLoaderBase,
        skill_ids: Optional[List[str]] = None,
    ) -> None:
        self._inner = inner
        # None = no filter (expose all); empty set = expose nothing
        self._skill_ids: Optional[set[str]] = (
            set(skill_ids) if skill_ids is not None else None
        )

    async def list_skills(self):
        skills = await self._inner.list_skills()
        if self._skill_ids is None or "*" in self._skill_ids:
            return skills
        return [s for s in skills if s.name in self._skill_ids]

    async def load_skill(self, name: str):
        # LocalSkillLoader does not expose load_skill — filter is applied at list_skills level.
        if self._skill_ids is not None and "*" not in self._skill_ids and name not in self._skill_ids:
            raise ValueError(f"Skill {name!r} is not in the allowed skill_ids whitelist.")
        if hasattr(self._inner, "load_skill"):
            return await self._inner.load_skill(name)
        raise NotImplementedError("Underlying loader does not support load_skill")


def list_skills_sync(loader: SkillLoaderBase) -> List[str]:
    """Synchronously list skill names via the async loader."""
    try:
        skills = asyncio.run(loader.list_skills())
        return [s.name for s in skills]
    except RuntimeError:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(asyncio.run, loader.list_skills())
            skills = fut.result(timeout=10)
        return [s.name for s in skills]
    except Exception as exc:
        logger.warning("list_skills failed: %s", exc)
        return []


def get_command_skill_bundles(
    skills_dir: Path | None = None,
    skill_ids: Optional[List[str]] = None,
) -> List[CommandKnowledgeBundle]:
    """Return validated command-knowledge bundles for the requested skills.

    Semantics mirror ``FilteredSkillLoader``:
    - skill_ids=None -> all command-knowledge skills
    - skill_ids=[] -> NO skills
    - skill_ids=["*"] -> all command-knowledge skills
    - skill_ids=[...] -> only named skills

    Directories without ``provenance.json`` are ordinary AgentScope skills and
    are intentionally skipped; only command-knowledge skills participate.
    """

    if skill_ids == []:
        return []
    names = None if skill_ids is None or "*" in skill_ids else skill_ids
    return discover_command_bundles(skills_dir, names=names)


def get_skill_guidance_prompt(
    skills_dir: Path | None = None,
    skill_ids: Optional[List[str]] = None,
) -> str:
    """Build a compact prompt-injection block from command-knowledge skills.

    The returned text contains instruction blocks only.  Provenance metadata
    and influence/license details are excluded so downstream prompts never
    mistake skill guidance for scientific evidence or citations.
    """

    bundles = get_command_skill_bundles(skills_dir, skill_ids)
    return "\n\n".join(bundle.to_prompt_block() for bundle in bundles)


def get_command_skill_bundle(
    skill_name: str,
    skills_dir: Path | None = None,
) -> CommandKnowledgeBundle:
    """Load one validated command-knowledge skill by name."""

    return load_command_bundle_by_name(skill_name, skills_dir=skills_dir)
