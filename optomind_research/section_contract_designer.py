"""SectionContractDesigner — S9: generate detailed argument contracts for every blueprint section.

Each contract has 11 required fields:
  section_purpose, central_thesis, argument_sequence, paragraph_functions,
  required_evidence_roles, expected_visual_roles, dependencies,
  forbidden_overclaims, transition_in, transition_out, word_budget, open_questions

M1 knowledge base is organizational mentor only; never used as evidence source.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.artifact_registry import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESIGNER_PROMPT = PROJECT_ROOT / "prompts" / "Section Contract Designer.txt"

SCHEMA_VERSION = "section_contracts.v1"
CONTRACT_SCHEMA = "section_contract.v1"


def _compact(value: Any, limit: int = 360) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_json_parse(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if match:
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except Exception:
                pass
    return {}


def _para_count(word_budget: int) -> int:
    """Estimate paragraph count: ~250 words per paragraph, minimum 2."""
    return max(2, round(word_budget / 250))


def _claim_text(value: Any) -> str:
    if isinstance(value, dict):
        return re.sub(r"\s+", " ", str(value.get("text", "") or "")).strip()
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fit_paragraph_functions(items: list[str], count: int) -> list[str]:
    """Return exactly one function per planned paragraph."""
    result = list(items[:count])
    while len(result) < count:
        n = len(result) + 1
        result.append(
            f"Para {n}: Develop one evidence-backed subclaim, then state its limitation or boundary."
        )
    return result


def _soften_evidence_role(value: Any) -> str:
    """Remove rigid paper-count quotas while preserving the evidence need."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"^At least\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+",
        "Prefer direct evidence from ",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _default_evidence_fallback_policy() -> dict[str, Any]:
    return {
        "preferred": "Same device class, mechanism, operating regime, and target constraint.",
        "acceptable": [
            "Same mechanism in an adjacent wavelength or material system, labeled indirect.",
            "Same inverse-design method in a closely related optical multilayer task, labeled method transfer.",
            "Authoritative background evidence used only for context.",
        ],
        "stop_rule": (
            "If direct evidence remains unavailable after targeted retrieval, qualify or narrow the claim "
            "and record an open question instead of fabricating support or searching indefinitely."
        ),
    }


_PAPER_QUOTA_PATTERN = re.compile(
    r"\b(?:at\s+least|fewer\s+than|no\s+fewer\s+than|minimum\s+of)\s+"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b",
    re.IGNORECASE,
)
_PLACEHOLDER_PATTERN = re.compile(
    r"(?:<\s*[A-Za-z][^>]{0,30}>|\b(?:TBD|to\s+be\s+filled|X\s*nm|Y[-\s]*degree)\b)",
    re.IGNORECASE,
)


def _validate_contract_planning_boundaries(
    contract: dict[str, Any], section: dict[str, Any], charter: dict[str, Any]
) -> None:
    """Reject quotas, placeholders, and scientific numbers absent from the input scope."""
    from optomind_research.blueprint_scientific_critic import _numeric_literals

    allowed_numbers = _numeric_literals({
        "charter_title": charter.get("title"),
        "central_question": charter.get("central_question"),
        "scope_statement": charter.get("scope_statement"),
        "section_title": section.get("section_title"),
        "purpose": section.get("purpose"),
        "planned_thesis": section.get("planned_thesis"),
    })
    scientific_payload = {
        key: contract.get(key)
        for key in (
            "section_title", "section_purpose", "central_thesis",
            "required_evidence_roles", "expected_visual_roles",
            "forbidden_overclaims", "open_questions", "evidence_fallback_policy",
        )
    }
    text = json.dumps(scientific_payload, ensure_ascii=False)
    unsupported = _numeric_literals(scientific_payload) - allowed_numbers
    if unsupported:
        raise ValueError(
            "contract introduced unsupported scientific numbers: "
            + ", ".join(sorted(unsupported))
        )
    if _PAPER_QUOTA_PATTERN.search(text):
        raise ValueError("contract introduced a rigid paper-count quota")
    if _PLACEHOLDER_PATTERN.search(text):
        raise ValueError("contract introduced an unresolved numeric placeholder")


def _evidence_roles(section: dict, central_thesis: str) -> list[str]:
    """Tailor evidence needs to the intellectual role instead of forcing numbers everywhere."""
    text = " ".join(
        [
            str(section.get("section_title", "")),
            str(section.get("purpose", "")),
            str(section.get("argument_role", "")),
            central_thesis,
        ]
    ).lower()
    roles = [
        "Prefer independent direct sources bearing on the planned thesis; if sparse, use clearly labeled mechanism or method-transfer evidence and narrow the claim."
    ]
    if any(k in text for k in ("mechanism", "physical", "explanation", "causal")):
        roles.append("A mechanism source that states the causal chain, assumptions, and boundary conditions.")
    if any(k in text for k in ("compare", "comparison", "contrast", "taxonomy", "category")):
        roles.append("Comparable evidence evaluated under explicit common criteria; note non-comparable conditions.")
    if any(k in text for k in ("performance", "quantitative", "benchmark", "efficiency", "temperature")):
        roles.append("A quantitative result with measurement conditions and uncertainty or limitations.")
    if any(k in text for k in ("history", "foundational", "development", "period")):
        roles.append("A primary or authoritative source establishing the timing and meaning of the claimed transition.")
    if any(k in text for k in ("gap", "limit", "challenge", "disagreement", "open")):
        roles.append("Evidence of the limitation or disagreement, including counterexamples or null results where available.")
    return roles[:4]


# ---------------------------------------------------------------------------
# Per-section contract builders
# ---------------------------------------------------------------------------

def _intro_contract(section: dict, charter: dict, blueprint: dict, section_titles: list[str]) -> dict:
    title = section.get("section_title", "Introduction")
    central_q = _compact(charter.get("central_question", ""), 200)
    next_title = section_titles[1] if len(section_titles) > 1 else "next section"
    word_budget = int(section.get("estimated_word_budget", 1200))
    n_paras = _para_count(word_budget)

    para_funcs = [
        f"Para 1: Establish the scientific or technological problem motivating the review.",
        f"Para 2: State why existing treatments are insufficient or incomplete.",
        f"Para 3: Introduce the central question: '{central_q[:80]}'.",
        f"Para 4: Preview the review's structural approach and what each major section contributes.",
    ]

    return {
        "schema_version": CONTRACT_SCHEMA,
        "section_role": "introduction",
        "section_id": section.get("section_id", "S01"),
        "section_index": section.get("section_index", 0),
        "section_title": title,
        "section_purpose": "Orient the reader to the problem, the gap in existing reviews, and the intellectual contribution of this review.",
        "central_thesis": f"Assess whether the literature supports a distinct synthesis addressing '{central_q[:100]}'.",
        "thesis_status": "planned",
        "argument_sequence": [
            "Step 1: Establish the scientific importance and practical stakes of the topic.",
            "Step 2: Characterize the gap in existing reviews (not just 'rapid progress' but a specific missing synthesis).",
            "Step 3: State the review's central question and scope explicitly.",
            "Step 4: Map the review's structure to its intellectual goals.",
        ],
        "paragraph_functions": _fit_paragraph_functions(para_funcs, n_paras),
        "required_evidence_roles": [
            "Independent high-quality papers or reviews that define the current state while leaving the central synthesis question unresolved.",
            "Evidence that existing reviews or primary literature leave the stated synthesis task unresolved.",
        ],
        "evidence_fallback_policy": _default_evidence_fallback_policy(),
        "expected_visual_roles": [
            "Optional: graphical abstract or schematic of the review's argument arc.",
        ],
        "dependencies": {
            "required_sections": [],
            "required_artifacts": ["S1_query_planning", "S5_review_charter"],
        },
        "forbidden_overclaims": [
            "Do not claim the field is 'rapidly advancing' without quantitative evidence.",
            "Do not promise findings the review does not deliver.",
        ],
        "transition_in": "N/A — this is the opening section.",
        "transition_out": (
            f"End on the unresolved scientific question that makes '{next_title}' necessary; "
            "do not announce the document structure."
        ),
        "continuity_guardrails": {
            "opening_mode": "introduce_review_once",
            "closing_mode": "open_argument_arc",
            "assumed_prior_knowledge": [],
            "concepts_introduced_here": [
                "review topic and standard abbreviation",
                "scope boundary",
                "central question",
                "organizing logic",
            ],
            "prohibited_reintroductions": [],
            "forbidden_section_local_closures": [
                "In summary", "To summarize", "Taken together",
                "These questions frame the subsequent analysis",
            ],
        },
        "word_budget": word_budget,
        "open_questions": [
            "Scope boundary: what tangential topics will readers expect that this review deliberately excludes?",
        ],
    }


def _body_contract(section: dict, charter: dict, prev_title: str, next_title: str, section_index: int) -> dict:
    title = section.get("section_title", f"Section {section_index + 1}")
    word_budget = int(section.get("estimated_word_budget", 1500))
    n_paras = _para_count(word_budget)

    # Derive central thesis from section metadata (argument_first has central_claim, chronological has period_thesis)
    raw_claim = (
        section.get("planned_thesis")
        or section.get("central_claim")
        or section.get("period_thesis")
        or section.get("purpose")
        or f"This section establishes the key findings related to '{_compact(title, 80)}'."
    )
    central_thesis = _claim_text(raw_claim)

    # Generate argument sequence based on section purpose
    purpose = _compact(section.get("purpose", ""), 200)
    arg_seq = [
        f"Step 1: Continue from the unresolved result inherited from '{prev_title}' without restating the review topic.",
        f"Step 2: Present the primary evidence or analytical framework.",
        f"Step 3: Evaluate competing interpretations or alternative findings.",
        f"Step 4: Synthesize into the section's central thesis: '{central_thesis[:80]}'.",
        f"Step 5: End on the scientific constraint or comparison that makes '{next_title}' necessary, without a mini-conclusion.",
    ]

    para_funcs = [
        "Para 1: Continuation — inherit the previous section's unresolved result without redefining the topic or abbreviation.",
        "Para 2: Primary evidence block — present the strongest support for the central thesis.",
        "Para 3: Mechanistic or theoretical interpretation of the evidence.",
        "Para 4: Evaluation of alternatives or conflicting evidence.",
        "Para 5: Integrative analysis — qualify the section thesis and expose its consequence for the next analytical problem.",
        "Para 6 (if budget allows): Develop limitations or open questions without a standalone summary paragraph.",
    ]

    return {
        "schema_version": CONTRACT_SCHEMA,
        "section_role": "body",
        "section_id": section.get("section_id", f"S{section_index + 1:02d}"),
        "section_index": section_index,
        "section_title": title,
        "section_purpose": purpose or f"Establish the key analytical contribution of '{title}'.",
        "central_thesis": central_thesis,
        "thesis_status": "planned",
        "argument_sequence": arg_seq[:5],
        "paragraph_functions": _fit_paragraph_functions(para_funcs, n_paras),
        "required_evidence_roles": _evidence_roles(section, central_thesis),
        "evidence_fallback_policy": _default_evidence_fallback_policy(),
        "expected_visual_roles": [
            f"Figure showing mechanism or evidence pattern for '{_compact(title, 60)}'.",
            "Table or comparison matrix if multiple approaches or findings are contrasted.",
        ],
        "dependencies": {
            "required_sections": [f"S{section_index:02d}"] if section_index > 0 else [],
            "required_artifacts": ["S3_kb_construction", "S10_evidence_portfolios"],
        },
        "forbidden_overclaims": [
            f"Do not claim '{central_thesis[:60]}' is universal without cross-system evidence.",
            "Do not use M1 intellectual-move patterns as factual support — they are organizational guides only.",
            "Do not assert a conclusion as settled if the evidence in the KB is preliminary or single-source.",
            "Do not redefine the review topic or repeat its standard abbreviation expansion.",
            "Do not close with a self-contained summary or announce subsequent sections.",
        ],
        "transition_in": (
            f"Continue directly from the unresolved question or finding in '{prev_title}'. "
            "Assume the review topic, scope, and abbreviations are already known."
        ),
        "transition_out": (
            f"End on the scientific need taken up by '{next_title}', without phrases such as "
            "'in summary', 'taken together', or 'the following section'."
        ),
        "continuity_guardrails": {
            "opening_mode": "continue_without_reintroduction",
            "closing_mode": "substantive_handoff_without_mini_conclusion",
            "assumed_prior_knowledge": [
                "review topic and standard abbreviation",
                "scope boundary and central question",
                f"the result established in '{prev_title}'",
            ],
            "concepts_introduced_here": [
                "only concepts uniquely required by this section's thesis"
            ],
            "prohibited_reintroductions": [
                "generic definition of the review topic",
                "standard abbreviation expansion already introduced",
                "review-wide motivation and scope preview",
                "background already owned by prior sections",
            ],
            "forbidden_section_local_closures": [
                "In summary", "To summarize", "Taken together", "Overall",
                "These questions frame the subsequent analysis",
                "The following section", "Subsequent sections",
            ],
        },
        "word_budget": word_budget,
        "open_questions": [
            f"What evidence would be needed to make '{central_thesis[:60]}' fully established?",
            "Are there negative results or null findings that should be acknowledged?",
        ],
    }


def _synthesis_contract(section: dict, charter: dict, prev_title: str, section_index: int) -> dict:
    title = section.get("section_title", "Synthesis and Future Directions")
    word_budget = int(section.get("estimated_word_budget", 1400))
    n_paras = _para_count(word_budget)
    central_q = _compact(charter.get("central_question", ""), 150)

    return {
        "schema_version": CONTRACT_SCHEMA,
        "section_role": "synthesis",
        "section_id": section.get("section_id", f"S{section_index + 1:02d}"),
        "section_index": section_index,
        "section_title": title,
        "section_purpose": "Integrate findings across all body sections into a unified answer to the review's central question; identify high-value open challenges.",
        "central_thesis": f"Determine the strongest qualified answer to '{central_q}' and retain only gaps justified by the assembled sections.",
        "thesis_status": "planned",
        "argument_sequence": [
            "Step 1: Summarize what each body section established (1 sentence each).",
            "Step 2: Identify the cross-section structure: how findings reinforce or qualify each other.",
            "Step 3: State the integrated answer to the central question.",
            "Step 4: Characterize the three most important remaining open challenges.",
            "Step 5: Suggest criteria for evaluating future work on these challenges.",
        ],
        "paragraph_functions": _fit_paragraph_functions([
            "Para 1: Cross-section synthesis — integrate the verdicts of all body sections.",
            "Para 2: Answer paragraph — state the review's answer to the central question explicitly.",
            "Para 3: Open challenge 1 — characterize, justify priority, suggest approach.",
            "Para 4: Open challenge 2.",
            "Para 5 (if budget): Open challenge 3 + closing statement on what would change the field.",
        ], n_paras),
        "required_evidence_roles": [
            "No new evidence should be introduced here — synthesis only.",
            "Forward-looking statements must cite speculative or preliminary findings explicitly.",
        ],
        "evidence_fallback_policy": _default_evidence_fallback_policy(),
        "expected_visual_roles": [
            "Optional: integrated schematic summarizing the review's argument arc.",
            "Optional: roadmap table of open challenges with priority and feasibility estimates.",
        ],
        "dependencies": {
            "required_sections": [f"S{i + 1:02d}" for i in range(section_index)],
            "required_artifacts": ["S5_review_charter", "S8_blueprint_selection"],
        },
        "forbidden_overclaims": [
            "Do not introduce new claims in the synthesis that were not established in body sections.",
            "Do not claim all open challenges are 'easily solvable' without evidence.",
            "Do not recapitulate the introduction — the synthesis must add to it.",
        ],
        "transition_in": f"Open by naming the cumulative finding from '{prev_title}' that makes a synthesis possible.",
        "transition_out": "N/A — this is the closing section.",
        "continuity_guardrails": {
            "opening_mode": "integrate_without_recapitulating_introduction",
            "closing_mode": "whole_review_synthesis",
            "assumed_prior_knowledge": [
                "all definitions, taxonomies, comparisons, and qualified section findings"
            ],
            "concepts_introduced_here": [
                "only the integrated answer and evidence-grounded research agenda"
            ],
            "prohibited_reintroductions": [
                "generic topic definition",
                "section-by-section chronological recap",
                "new factual evidence absent from body sections",
            ],
            "forbidden_section_local_closures": [],
        },
        "word_budget": word_budget,
        "open_questions": [
            "Are the three prioritized open challenges genuinely the most important, or did the review's scope filter out higher-priority ones?",
        ],
    }


def _build_contracts(sections: list[dict], charter: dict) -> list[dict]:
    """Build one contract per section."""
    n = len(sections)
    titles = [s.get("section_title", f"Section {i}") for i, s in enumerate(sections)]
    contracts = []
    for i, s in enumerate(sections):
        prev_title = titles[i - 1] if i > 0 else "N/A"
        next_title = titles[i + 1] if i < n - 1 else "N/A"
        role = str(s.get("section_role") or "").strip().lower()
        inferred_role = "introduction" if i == 0 else "synthesis" if i == n - 1 else "body"
        if role and role != inferred_role:
            raise ValueError(
                f"Section {s.get('section_id', i)} has role {role!r}; expected {inferred_role!r}."
            )
        is_intro = inferred_role == "introduction"
        is_final = inferred_role == "synthesis"

        if is_intro:
            contract = _intro_contract(s, charter, {}, titles)
        elif is_final:
            contract = _synthesis_contract(s, charter, prev_title, i)
        else:
            contract = _body_contract(s, charter, prev_title, next_title, i)

        contracts.append(contract)
    return contracts


# ---------------------------------------------------------------------------
# SectionContractDesigner
# ---------------------------------------------------------------------------

@dataclass
class SectionContractDesigner:
    """Produces detailed argument contracts for each section in the selected blueprint."""

    prompt_path: Path = field(default_factory=lambda: DEFAULT_DESIGNER_PROMPT)
    model_tier: str = "advanced_model"
    real_llm: bool = False

    def design_contracts(
        self,
        *,
        charter: dict,
        blueprint: dict,
    ) -> dict[str, Any]:
        """Return a section_contracts.v1 dict."""
        sections = list(blueprint.get("sections") or [])
        if not sections:
            return {
                "schema_version": SCHEMA_VERSION,
                "created_at": utc_now(),
                "mode": "deterministic",
                "section_count": 0,
                "contracts": [],
                "error": "Blueprint has no sections.",
            }

        contracts = (
            self._llm_contracts(charter=charter, blueprint=blueprint, sections=sections)
            if self.real_llm
            else _build_contracts(sections, charter)
        )
        if not self.real_llm:
            for index, (section, contract) in enumerate(zip(sections, contracts)):
                planned_thesis = _claim_text(
                    section.get("planned_thesis")
                    or section.get("central_claim")
                    or section.get("period_thesis")
                )
                contract["section_id"] = str(section.get("section_id") or f"S{index + 1:02d}")
                contract["section_index"] = index
                if planned_thesis:
                    contract["central_thesis"] = planned_thesis
                    contract["thesis_source"] = "blueprint_planned_thesis"
                contract["thesis_status"] = "planned"
                contract["argument_sequence_status"] = "planning_instructions_not_factual_findings"
            from optomind_research.intermediate_language_guard import ensure_english_payload
            contracts = ensure_english_payload(contracts)
        total_words = sum(c.get("word_budget", 0) for c in contracts)

        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "mode": "real_llm" if self.real_llm else "deterministic",
            "section_count": len(contracts),
            "total_word_budget": total_words,
            "structural_logic": blueprint.get("structural_logic", ""),
            "contracts": contracts,
        }

    def _llm_contracts(self, *, charter: dict, blueprint: dict, sections: list[dict]) -> list[dict]:
        if not Path(self.prompt_path).exists():
            raise FileNotFoundError(f"SectionContractDesigner prompt not found: {self.prompt_path}")
        system_prompt = Path(self.prompt_path).read_text(encoding="utf-8").strip()
        expected_ids = [str(s.get("section_id") or f"S{i + 1:02d}") for i, s in enumerate(sections)]
        required = {
            "section_purpose", "central_thesis", "argument_sequence", "paragraph_functions",
            "required_evidence_roles", "expected_visual_roles", "dependencies",
            "forbidden_overclaims", "transition_in", "transition_out", "word_budget", "open_questions",
        }

        blueprint_context = {
            "structural_logic": blueprint.get("structural_logic", ""),
            "section_order": [
                {
                    "section_id": s.get("section_id"),
                    "section_title": s.get("section_title"),
                    "section_role": (
                        "introduction" if i == 0
                        else "synthesis" if i == len(sections) - 1
                        else "body"
                    ),
                    "purpose": s.get("purpose"),
                    "planned_thesis": s.get("planned_thesis"),
                }
                for i, s in enumerate(sections)
            ],
        }

        def normalize(index: int, contract: dict) -> dict:
            section = sections[index]
            if not isinstance(contract, dict) or required - set(contract):
                raise RuntimeError(f"Section contract {index} is missing required fields.")
            contract = dict(contract)
            contract["schema_version"] = CONTRACT_SCHEMA
            contract["section_id"] = expected_ids[index]
            contract["section_index"] = index
            contract["section_title"] = section.get("section_title", contract.get("section_title", ""))
            inferred_role = (
                "introduction" if index == 0
                else "synthesis" if index == len(sections) - 1
                else "body"
            )
            contract["section_role"] = inferred_role
            contract.setdefault(
                "continuity_guardrails",
                {
                    "opening_mode": (
                        "introduce_review_once" if inferred_role == "introduction"
                        else "integrate_without_recapitulating_introduction"
                        if inferred_role == "synthesis"
                        else "continue_without_reintroduction"
                    ),
                    "closing_mode": (
                        "whole_review_synthesis" if inferred_role == "synthesis"
                        else "open_argument_arc" if inferred_role == "introduction"
                        else "substantive_handoff_without_mini_conclusion"
                    ),
                    "assumed_prior_knowledge": [] if inferred_role == "introduction" else [
                        "review topic, scope, abbreviations, and prior section findings"
                    ],
                    "concepts_introduced_here": [],
                    "prohibited_reintroductions": [] if inferred_role == "introduction" else [
                        "generic topic definition",
                        "standard abbreviation expansion",
                        "review-wide motivation and scope preview",
                    ],
                    "forbidden_section_local_closures": [] if inferred_role == "synthesis" else [
                        "In summary", "To summarize", "Taken together",
                        "These questions frame the subsequent analysis",
                    ],
                },
            )
            contract["thesis_status"] = "planned"
            planned_thesis = _claim_text(
                section.get("planned_thesis")
                or section.get("central_claim")
                or section.get("period_thesis")
            )
            if planned_thesis:
                contract["central_thesis"] = planned_thesis
                contract["thesis_source"] = "blueprint_planned_thesis"
            contract["argument_sequence_status"] = "planning_instructions_not_factual_findings"
            contract["required_evidence_roles"] = [
                _soften_evidence_role(value)
                for value in (contract.get("required_evidence_roles") or [])
                if str(value or "").strip()
            ]
            contract.setdefault("evidence_fallback_policy", _default_evidence_fallback_policy())
            contract["word_budget"] = int(section.get("estimated_word_budget") or contract.get("word_budget") or 0)
            contract["paragraph_functions"] = _fit_paragraph_functions(
                list(contract.get("paragraph_functions") or []),
                _para_count(contract["word_budget"]),
            )
            _validate_contract_planning_boundaries(contract, section, charter)
            return contract

        def call_one(index: int) -> tuple[int, dict]:
            from llm.qwen_chat_client import call_qwen_chat

            section = sections[index]
            attempts: list[dict[str, Any]] = []
            last_error = ""
            for attempt in range(2):
                payload = {
                    "charter": charter,
                    "blueprint_context": blueprint_context,
                    "section": section,
                    "previous_section": sections[index - 1] if index > 0 else None,
                    "next_section": sections[index + 1] if index + 1 < len(sections) else None,
                    "single_section_mode": True,
                    "epistemic_boundary": (
                        "This stage plans how to test the section thesis. It has no authority to add "
                        "scientific findings, numerical values, named standards, or consensus claims."
                    ),
                }
                if attempt:
                    payload["repair_instruction"] = (
                        "Return one complete JSON object under the key 'section_contract'. "
                        "Preserve section_id and keep all lists concise. Correct the prior validation error: "
                        + last_error
                    )
                try:
                    result = call_qwen_chat(
                        f"SectionContractDesigner-{expected_ids[index]}",
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                        ],
                        model_tier=self.model_tier,
                        temperature=0.15,
                        max_tokens=3200,
                        response_format={"type": "json_object"},
                        stream=True,
                        force_mock=False,
                        max_retries=1,
                    )
                    raw = str(result.get("content") or "")
                    parsed = _safe_json_parse(raw)
                    contract = parsed.get("section_contract")
                    if not isinstance(contract, dict):
                        rows = parsed.get("section_contracts") or parsed.get("contracts") or []
                        contract = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else parsed
                    attempts.append({"attempt": attempt + 1, "raw_chars": len(raw), "parsed": bool(parsed)})
                    return index, normalize(index, contract)
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    attempts.append({"attempt": attempt + 1, "error": f"{type(exc).__name__}: {exc}"})
            raise RuntimeError(f"{expected_ids[index]} contract generation failed: {attempts}")

        by_index: dict[int, dict] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=min(4, len(sections))) as pool:
            futures = {pool.submit(call_one, index): index for index in range(len(sections))}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    returned_index, contract = future.result()
                    by_index[returned_index] = contract
                except Exception as exc:
                    errors.append(f"{expected_ids[index]}: {exc}")
        if errors or len(by_index) != len(sections):
            raise RuntimeError("SectionContractDesigner parallel generation failed; " + " | ".join(errors))

        normalised = [by_index[index] for index in range(len(sections))]
        from optomind_research.intermediate_language_guard import ensure_english_payload
        return ensure_english_payload(normalised)
