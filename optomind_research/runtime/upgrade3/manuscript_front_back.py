"""Front/back part generation and final assembly (work order 03).

Strictly serial by content dependency: the conclusion is distilled from the
actual body first; only after its response is PARSED is the introduction
message built (so it reads the freshly generated conclusion); only after the
introduction is parsed is the abstract/keywords/title message built (so it
reads both updated parts).  Providers are replay or labeled manual fixtures.
Empty bodies and ``offline_placeholder`` responses are rejected as failures,
never recorded as generated.

With a manuscript-parts contract, application owns only marked spans.
A clear unowned article-role location conflicts with a new standalone part
and blocks generation/application without changing BODY.

Legacy fixture application keeps ONE main position per part: an existing
摘要/引言/结语 section body is replaced in place.  When the body's first chapter already
serves as the introduction (its title/role says so), the generated
introduction updates THAT chapter's opening instead of adding a second
"## 引言" section.  A missing conclusion is inserted before the references
section, never after it.  Keywords are written into the manuscript attached
to the abstract.  Manuscripts over the single-pass limit are rejected
explicitly — chapter-wise reading is not implemented and must not be claimed.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

STAGE_ORDER = ("conclusion", "introduction", "abstract")
FULL_TEXT_SINGLE_PASS_CHAR_LIMIT = 400_000
_PLACEHOLDER_MARKERS = ("offline_placeholder", "offline placeholder")


class FrontBackError(ValueError):
    pass


# This is an input projection, not another planning model or literature router.
_CONTEXT_FIELDS = ("research_question", "review_argument", "shared_scope",
                   "material_theme_inventory", "source_identity_map",
                   "manuscript_parts_plan", "material_records", "material_access",
                   "status", "manuscript_parts_plan_frozen", "manuscript_parts_plan_revision")
MATERIAL_RECORD_CHAR_LIMIT = 100_000


def normalize_planning_context(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate an explicitly supplied new contract; never infer one from history.

    Material records are actual supplied excerpts/cards. Access paths are only
    locators, and must never be represented to the model as already read text.
    """
    if value is None:
        return None
    from .manuscript_parts import validate_manuscript_parts_plan
    if not isinstance(value, Mapping):
        raise FrontBackError("planning_context_not_object")
    if "status" in value and value["status"] != "complete":
        raise FrontBackError("planning_context_not_complete")
    if value.get("manuscript_parts_plan_frozen") is False:
        raise FrontBackError("planning_context_not_frozen")
    if value.get("manuscript_parts_plan_revision") in ("v0", "v1", 0, 1):
        raise FrontBackError("planning_context_revision_not_final")
    result = {key: copy.deepcopy(value[key]) for key in _CONTEXT_FIELDS if key in value}
    result["manuscript_parts_plan"] = validate_manuscript_parts_plan(
        value.get("manuscript_parts_plan"))
    records = result.get("material_records", [])
    if not isinstance(records, list) or any(not isinstance(row, Mapping) for row in records):
        raise FrontBackError("material_records_not_object_list")
    if len(json.dumps(records, ensure_ascii=False)) > MATERIAL_RECORD_CHAR_LIMIT:
        raise FrontBackError("material_records_exceed_bounded_input_limit")
    result["material_records"] = records
    return result


def _unsupported_placements(context: Mapping[str, Any]) -> list[dict[str, str]]:
    return [{"stage": stage, "mode": part["placement"]["mode"],
             "anchor": part["placement"]["anchor"], "error": "unsupported_placement"}
            for stage, part in context["manuscript_parts_plan"].items()
            if stage in STAGE_ORDER and part["placement"]["mode"] != "standalone"]


def _duty(stage: str) -> str:
    if stage == "conclusion":
        return (
            "提炼这篇综述**实际建立的认识**与边界：先按正文各部分的实际份量综合跨主题的共同认识、"
            "互补关系或分歧，再说明足以改变解释范围的关键限制、未决问题与合理方向。职责卡中的 focus "
            "和 boundary 是待处理任务与取舍边界，不是要求逐条写成固定结论或限制清单；不能让少数局部缺口"
            "替代全文综合。结语服务正文，不复述章节目录，不罗列每章小节，允许基于正文证据形成新的综合判断，"
            "但不新增无依据的经验性主张或宽泛展望。"
        )
    if stage == "introduction":
        return (
            "建立研究背景、当前已有的主要认识、尚待综合之处，以及本综述的范围、组织视角与贡献；"
            "可以简述各部分分工的理由来说明阅读路径，但不逐章列目录，不抢正文深讲，不先搬用结语或逐条复述结论。"
            "按正文实际材料与份量校准承诺，只许诺正文实际兑现的内容，并与已提炼的结语保持一致。"
            "不写成全篇摘要，不做项目宣传，不夸大证据强度。"
        )
    return (
        "写摘要与关键词：摘要按正文实际份量自含地提炼主要认识及其关系，并用必要限度说明边界，"
        "不写成限制清单或职责卡的逐条复述；职责卡中的 focus 和 boundary 只指导取舍，不要求每项都转写为固定结论。"
        "具体格式遵循职责卡中已确认的文体要求；不写成章节目录；关键词选取正文实际核心概念。"
        "题名和摘要不凭组织方式、篇幅或材料数量新增研究设计或方法文体标签；只有输入实际说明了相应的研究、"
        "检索筛选或综合方法时才使用此类标签，既有题名中的标签本身不是依据；"
        "题名仍须概括正文实际范围，不夸大。"
    )


def build_stage_messages(
    stage: str,
    *,
    body_text: str,
    research_question: str,
    chapter_roles: Sequence[Mapping[str, Any]],
    prior_parts: Mapping[str, Any],
    language: str = "zh",
    planning_context: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """One stage's real message, carrying the parts already parsed."""
    if len(body_text) > FULL_TEXT_SINGLE_PASS_CHAR_LIMIT:
        raise FrontBackError(
            f"manuscript_too_long_for_single_pass:{len(body_text)}>"
            f"{FULL_TEXT_SINGLE_PASS_CHAR_LIMIT}:chapter_wise_reading_not_implemented")
    roles_block = json.dumps(
        [{"chapter_id": row.get("chapter_id"), "title": row.get("title"),
          "role": row.get("role")} for row in chapter_roles],
        ensure_ascii=False, indent=2)
    user = (
        f"【研究问题】\n{research_question}\n\n"
        f"【章节职责】\n{roles_block}\n\n"
        f"【正文当前全文】\n{body_text}"
    )
    context = normalize_planning_context(planning_context)
    if context is not None:
        plan = context["manuscript_parts_plan"]
        stage_context = {key: val for key, val in context.items()
                         if key != "manuscript_parts_plan"}
        stage_context["context"] = plan["context"]
        stage_context["part_plan"] = plan[stage]
        user += "\n\n【早期职责与最终科研理解】\n" + json.dumps(
            stage_context, ensure_ascii=False, indent=2)
        user += ("\n职责卡规定认识任务和边界，不是不可修改的结论。以实际正文、scope和真实材料校准主张；"
                 "material_records是本轮提供的材料，material_access与card_path只是未必已读取的定位信息，"
                 "不得把路径当作科学证据。深度教学与证据综合正文无论标题是什么均须保留。"
                 "引用仅用提供身份对应的原始source handle，勿创造来源。")
    if stage == "introduction" and prior_parts.get("conclusion"):
        user += "\n\n【已提炼的结语（引言承诺须与之一致）】\n" + prior_parts["conclusion"]
    if stage == "abstract":
        if prior_parts.get("conclusion"):
            user += "\n\n【已提炼的结语】\n" + prior_parts["conclusion"]
        if prior_parts.get("introduction"):
            user += "\n\n【已写好的引言】\n" + prior_parts["introduction"]
    user += f"\n\n【本轮任务】{_duty(stage)}"
    user += "\n\n【本轮交付】只返回一个 JSON 对象："
    if stage == "abstract":
        user += '{"title": "...", "abstract": "...", "keywords": ["...", "..."]}。'
    else:
        user += f'{{"{stage}": "..."}}。'
    requested_language = str(language or "zh").strip()
    language_label = {"zh": "中文", "en": "English"}.get(
        requested_language.lower(), requested_language)
    return [
        {"role": "system",
         "content": (
             "你是这篇完整综述的定稿编辑，只负责首尾部件，不改正文章节正文。"
             f"输出语言要求：{language_label}。"
         )},
        {"role": "user", "content": user},
    ]


def build_front_back_passes(
    *,
    body_text: str,
    research_question: str,
    chapter_roles: Sequence[Mapping[str, Any]],
    existing_front: str = "",
    existing_back: str = "",
    language: str = "zh",
) -> list[dict[str, Any]]:
    """Compatibility helper: pre-build all three stages WITHOUT prior parts.

    Kept only for callers that inspect the message shapes; the real runner
    (:func:`run_front_back_stage`) builds each stage after the previous one is
    parsed, so the introduction reads the fresh conclusion and the abstract
    reads both fresh parts.  Do not use this for generation.
    """

    return [{"stage": stage,
             "messages": build_stage_messages(
                 stage, body_text=body_text, research_question=research_question,
                 chapter_roles=chapter_roles, prior_parts={}, language=language),
             "note": "prebuilt_without_prior_parts_debug_only"}
            for stage in STAGE_ORDER]


def parse_front_back_response(stage: str, response: Any) -> dict[str, Any]:
    """Parse one stage response; empty or placeholder parts are failures."""

    content = response.get("content") if isinstance(response, Mapping) else response
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise FrontBackError(f"front_back_not_json:{stage}:{exc}") from exc
    elif isinstance(response, Mapping):
        data = dict(response)
    else:
        raise FrontBackError(f"front_back_unreadable:{stage}")
    if not isinstance(data, Mapping):
        raise FrontBackError(f"front_back_not_object:{stage}")
    lowered = json.dumps(data, ensure_ascii=False).casefold()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        raise FrontBackError(f"front_back_placeholder_rejected:{stage}")
    if stage == "abstract":
        part = {"title": str(data.get("title") or "").strip(),
                "abstract": str(data.get("abstract") or "").strip(),
                "keywords": [str(k).strip() for k in (data.get("keywords") or [])
                             if str(k).strip()]}
        if not part["abstract"] or not part["keywords"]:
            raise FrontBackError(f"front_back_empty_part:{stage}")
        return part
    body = str(data.get(stage) or "").strip()
    if not body:
        raise FrontBackError(f"front_back_empty_part:{stage}")
    return {stage: body}


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------


def _replace_section(manuscript: str, heading_pattern: str, new_body: str) -> tuple[str, bool]:
    """Replace one section's body in place; False when the heading is absent."""

    pattern = re.compile(
        r"(^#{2,3}\s+" + heading_pattern + r"\s*$)\n(.*?)(?=^#{2,3}\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(manuscript)
    if not match:
        return manuscript, False
    return (manuscript[:match.start(2)] + new_body.strip() + "\n\n"
            + manuscript[match.end(2):], True)


def _insert_after_title(manuscript: str, block: str, *, after_abstract: bool) -> str:
    """Insert a block once in the front area, in conventional reading order."""

    lines = manuscript.splitlines(keepends=True)
    title_idx = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if title_idx is None:
        return manuscript.rstrip() + "\n\n" + block
    section_idx = next((i for i in range(title_idx + 1, len(lines))
                        if lines[i].startswith("## ")), None)
    if section_idx is None:
        return "".join(lines).rstrip() + "\n\n" + block
    if after_abstract:
        i = section_idx + 1
        while i < len(lines):
            if lines[i].startswith("## "):
                return "".join(lines[:i]) + block + "".join(lines[i:])
            i += 1
        return "".join(lines).rstrip() + "\n\n" + block
    return "".join(lines[:section_idx]) + block + "".join(lines[section_idx:])


def _is_intro_role(role: str) -> bool:
    role = str(role or "")
    return "引言" in role or "introduction" in role.casefold()


def _introduction_chapter_heading(
    manuscript: str,
    chapter_roles: Sequence[Mapping[str, Any]],
) -> str:
    """The heading line of the body chapter that serves as the introduction.

    Resolution order (explicit identity before bounded inference):
    1. An introduction role's chapter_id, matched as a token rather than a
       substring (so ``CH01`` cannot resolve ``CH010``).
    2. An introduction role's configured title, matched exactly against the
       heading text.
    3. A positional chapter-role mapping only when the document exposes a
       complete, reliably numbered chapter sequence and no explicit
       introduction identity was left unresolved.
    4. A heading whose title says 引言/Introduction.

    This finds the right chapter even when its title does not contain the
    word 引言, as long as the role mapping says it is the intro chapter.
    """
    headings = [line.strip() for line in manuscript.splitlines()
                if line.startswith("## ")]

    intro_rows = [row for row in chapter_roles
                  if _is_intro_role(row.get("role"))]

    def _id_matches(heading: str, chapter_id: str) -> bool:
        # IDs are normally CH##, but keep this generic while requiring token
        # boundaries so a shorter ID cannot match a longer one.
        return bool(re.search(
            rf"(?<![A-Za-z0-9]){re.escape(chapter_id)}(?![A-Za-z0-9])",
            heading,
        ))

    def _title_matches(heading: str, title: str) -> bool:
        heading_title = heading[3:].strip()
        if heading_title == title:
            return True
        # Existing chapter-role manifests store the title without the
        # document's ``第N章``/``Chapter N`` prefix.  Treat that prefix as
        # structural while keeping the configured title itself exact.
        bare_title = re.sub(
            r"^(?:第[0-9一二三四五六七八九十百]+章|Chapter\s+[0-9]+)\s*",
            "",
            heading_title,
            flags=re.IGNORECASE,
        )
        return bare_title == title

    # Resolve every explicit ID before considering titles or document order.
    unresolved_explicit_id = False
    for row in intro_rows:
        chapter_id = str(row.get("chapter_id") or "").strip()
        if not chapter_id:
            continue
        matches = [heading for heading in headings
                   if _id_matches(heading, chapter_id)]
        if len(matches) == 1:
            return matches[0]
        unresolved_explicit_id = True

    # A title is a reliable identity even when the body uses English headings
    # or omits the configured chapter ID altogether.
    for row in intro_rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        matches = [heading for heading in headings
                   if _title_matches(heading, title)]
        if len(matches) == 1:
            return matches[0]

    if unresolved_explicit_id:
        return ""

    # Do not guess by position after an explicit ID failed to resolve.  When
    # no explicit identity is available, require a complete numbered sequence
    # before using the role index as a bounded fallback.
    chapter_like = [
        h for h in headings
        if re.search(r"(?:第[0-9一二三四五六七八九十百]+章|Chapter\s+[0-9]+)", h,
                     re.IGNORECASE)
        or any(_id_matches(h, str(row.get("chapter_id") or "").strip())
               for row in chapter_roles if str(row.get("chapter_id") or "").strip())
    ]
    numbered_sequence = bool(chapter_like) and len(chapter_like) == len(chapter_roles)
    if not unresolved_explicit_id and numbered_sequence:
        for index, row in enumerate(chapter_roles):
            if _is_intro_role(row.get("role")):
                return chapter_like[index]

    for heading in headings:
        title = heading.lstrip("# ").strip()
        if "引言" in title or "introduction" in title.casefold():
            return heading
    return ""


_GENERATED_INTRO_START = "<!-- generated-introduction-start -->"
_GENERATED_INTRO_END = "<!-- generated-introduction-end -->"


def _update_chapter_opening(
    manuscript: str,
    chapter_heading: str,
    intro_text: str,
) -> tuple[str, str]:
    """Write the generated introduction as the chapter's opening.

    The generated opening is wrapped in marker comments so a later run can
    REPLACE it in place (a changed introduction keeps only the new version)
    instead of inserting a second copy.  The chapter's own thesis quote
    (``> 本章论断…``) and body paragraphs are never touched.  Returns
    ``(new_manuscript, position)`` where position is
    ``body_introduction_chapter_opening_replaced`` or ``..._inserted``.
    """
    heading_idx = manuscript.find(chapter_heading)
    if heading_idx < 0:
        return manuscript, "introduction_chapter_heading_not_found"
    span_end = manuscript.find("\n## ", heading_idx + len(chapter_heading))
    if span_end < 0:
        span_end = len(manuscript)
    chapter_span = manuscript[heading_idx:span_end]

    wrapped = f"{_GENERATED_INTRO_START}\n\n{intro_text.strip()}\n\n{_GENERATED_INTRO_END}"
    marker = re.compile(
        re.escape(_GENERATED_INTRO_START) + r".*?" + re.escape(_GENERATED_INTRO_END),
        re.DOTALL)
    if marker.search(chapter_span):
        new_span = marker.sub(lambda _m: wrapped, chapter_span, count=1)
        return (manuscript[:heading_idx] + new_span + manuscript[span_end:],
                "body_introduction_chapter_opening_replaced")

    after = heading_idx + len(chapter_heading)
    rest = manuscript[after:span_end]
    # Skip the chapter's blockquote thesis line ("> ...") if present.
    quote = re.match(r"\n(>[^\n]*(\n>[^\n]*)*)", rest)
    if quote:
        after += quote.end()
    insertion = "\n\n" + wrapped + "\n\n"
    return (manuscript[:after].rstrip("\n") + insertion + manuscript[after:].lstrip("\n"),
            "body_introduction_chapter_opening_inserted")


def _insert_before_references(manuscript: str, block: str) -> str:
    """Insert the conclusion before the references section, not after it."""

    pattern = re.compile(r"^#{2,3}\s+(?:参考文献|References)", re.MULTILINE)
    match = pattern.search(manuscript)
    if match:
        return manuscript[:match.start()].rstrip() + "\n\n" + block.strip() + "\n\n" \
            + manuscript[match.start():]
    return manuscript.rstrip() + "\n\n" + block.strip() + "\n"


def _write_keywords(manuscript: str, keywords: Sequence[str]) -> str:
    """Attach the keyword line to the abstract section (replace or append)."""

    line = "**关键词：** " + "；".join(keywords)
    existing = re.compile(r"^\*\*关键词[：:]\*\*.*$", re.MULTILINE)
    if existing.search(manuscript):
        return existing.sub(line, manuscript, count=1)
    abstract_pattern = re.compile(
        r"(^#{2,3}\s+(?:摘要|Abstract)\s*$)", re.MULTILINE)
    match = abstract_pattern.search(manuscript)
    if match:
        end = manuscript.find("\n## ", match.end())
        if end < 0:
            end = len(manuscript)
        return manuscript[:end].rstrip() + "\n\n" + line + "\n\n" + manuscript[end:]
    return manuscript.rstrip() + "\n\n" + line + "\n"


def apply_front_back(
    manuscript: str,
    parts: Mapping[str, Any],
    chapter_roles: Sequence[Mapping[str, Any]] = (),
    planning_context: Mapping[str, Any] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Write each part into its single main position; keep 展望 as is.

    Returns ``(new_manuscript, application_log)`` where the log records where
    each part landed (useful for acceptance reading).
    """

    context = normalize_planning_context(planning_context)
    if context is not None:
        if _unsupported_placements(context):
            raise FrontBackError("unsupported_placement")
        conflicts, _ = _placement_conflicts(manuscript, chapter_roles, context)
        if conflicts:
            raise FrontBackError("placement_conflict")
        return _apply_owned_parts(manuscript, parts)
    result = manuscript
    log: list[dict[str, str]] = []
    title = str(parts.get("title") or "").strip()
    if title:
        result = re.sub(r"^# .*$", "# " + title, result, count=1, flags=re.MULTILINE)
        log.append({"part": "title", "position": "h1_replaced"})
    abstract = str(parts.get("abstract") or "").strip()
    if abstract:
        result, replaced = _replace_section(result, r"(?:摘要|Abstract)", abstract)
        if replaced:
            log.append({"part": "abstract", "position": "existing_section_updated"})
        else:
            result = _insert_after_title(
                result, "## 摘要\n\n" + abstract + "\n\n", after_abstract=False)
            log.append({"part": "abstract", "position": "inserted_after_title"})
    keywords = [str(k).strip() for k in (parts.get("keywords") or []) if str(k).strip()]
    if keywords:
        result = _write_keywords(result, keywords)
        log.append({"part": "keywords", "position": "attached_to_abstract"})
    introduction = str(parts.get("introduction") or "").strip()
    if introduction:
        # A pre-existing standalone 引言 section is updated in place; the
        # body's own introduction chapter otherwise gets the generated text
        # as its opening (replaced on later runs, never duplicated).
        result, replaced = _replace_section(
            result, r"(?:引言|绪论|Introduction)", introduction)
        if replaced:
            log.append({"part": "introduction", "position": "existing_section_updated"})
        else:
            chapter_heading = _introduction_chapter_heading(result, chapter_roles)
            if chapter_heading:
                result, position = _update_chapter_opening(
                    result, chapter_heading, introduction)
                log.append({"part": "introduction", "position": position,
                            "chapter_heading": chapter_heading})
            else:
                result = _insert_after_title(
                    result, "## 引言\n\n" + introduction + "\n\n", after_abstract=True)
                log.append({"part": "introduction", "position": "inserted_after_abstract"})
    conclusion = str(parts.get("conclusion") or "").strip()
    if conclusion:
        result, replaced = _replace_section(
            result, r"(?:结语|结论|总结|Conclusion)", conclusion)
        if replaced:
            log.append({"part": "conclusion", "position": "existing_section_updated"})
        else:
            result = _insert_before_references(result, "## 结语\n\n" + conclusion)
            log.append({"part": "conclusion", "position": "inserted_before_references"})
    return result, log




_PART_HEADING_NAMES = {
    "abstract": {"abstract", "摘要"},
    "introduction": {"introduction", "引言", "绪论"},
    "conclusion": {"conclusion", "conclusions", "结语", "结论", "总结"},
}
_PART_ROLE_NAMES = {
    "abstract": _PART_HEADING_NAMES["abstract"],
    "introduction": _PART_HEADING_NAMES["introduction"] | {"opening", "introduction-like", "入口"},
    "conclusion": _PART_HEADING_NAMES["conclusion"] | {"closing", "conclusion-like", "收束"},
}
_CHAPTER_PREFIX = re.compile(
    r"^(?:第[0-9一二三四五六七八九十百]+章|Chapter\s+[0-9]+|CH[0-9]+)\s*[:：.、-]?\s*",
    re.IGNORECASE,
)


def _unowned_article_headings(manuscript: str) -> list[str]:
    """Inspect publication locations only; never classify or change BODY tasks.

    Valid owned spans are excluded, as are fenced code examples and headings
    below article/chapter level. In particular, a mere occurrence of the word
    Introduction in a technical title does not establish an article role.
    """
    unowned = re.sub(r"<!-- manuscript-part:(abstract|introduction|conclusion):start -->.*?"
                     r"<!-- manuscript-part:\1:end -->", "", manuscript, flags=re.DOTALL)
    headings = []
    fence = ""
    for line in unowned.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if not fence and re.match(r"^ {0,3}#{1,2}\s+", line):
            headings.append(line.strip())
    return headings


def _heading_title(heading: str) -> str:
    return re.sub(r"\s+#+\s*$", "", heading.lstrip("# ").strip()).strip()


def _placement_conflicts(
    manuscript: str,
    chapter_roles: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Fail closed only on deterministic standalone publication collisions."""
    headings = _unowned_article_headings(manuscript)
    conflicts: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def record(part: str, location: str) -> None:
        if (part, location) not in seen:
            seen.add((part, location))
            conflicts.append({"part": part, "requested_mode": "standalone",
                              "existing_location": location,
                              "reason": "existing_unowned_body_location"})

    for part in STAGE_ORDER:
        if context["manuscript_parts_plan"][part]["placement"]["mode"] != "standalone":
            continue
        # A role is meaningful only when it resolves to an actual unowned
        # chapter location. Stale or ambiguous metadata alone cannot block.
        for row in chapter_roles:
            if str(row.get("role") or "").strip().casefold() not in _PART_ROLE_NAMES[part]:
                continue
            chapter_id = str(row.get("chapter_id") or "").strip()
            title = str(row.get("title") or "").strip()
            matches = [h for h in headings if chapter_id and re.search(
                rf"(?<![A-Za-z0-9]){re.escape(chapter_id)}(?![A-Za-z0-9])", _heading_title(h))]
            if len(matches) != 1:
                matches = [h for h in headings if title and
                           (_heading_title(h) == title or _CHAPTER_PREFIX.sub("", _heading_title(h)) == title)]
            if len(matches) != 1 and re.fullmatch(r"CH[0-9]+", chapter_id, re.IGNORECASE):
                number = int(chapter_id[2:])
                matches = [h for h in headings if re.match(
                    rf"^(?:第0*{number}章|Chapter\s+0*{number}(?![0-9]))(?:\s|[:：.、-]|$)",
                    _heading_title(h), re.IGNORECASE)]
            if len(matches) == 1:
                record(part, matches[0])
            else:
                warnings.append({"part": part, "chapter_id": chapter_id,
                                 "status": "unresolved_part_role",
                                 "message": "Role does not resolve to one unowned location; BODY preserved."})
        for heading in headings:
            title = _CHAPTER_PREFIX.sub("", _heading_title(heading)).casefold()
            if title in _PART_HEADING_NAMES[part]:
                record(part, heading)
            elif any(re.match(re.escape(name) + r"(?:\s|[:：])", title)
                     for name in _PART_HEADING_NAMES[part]):
                warnings.append({"part": part, "heading": heading,
                                 "status": "ambiguous_part_heading",
                                 "message": "Technical heading alone does not establish article responsibility; BODY preserved."})
    return conflicts, warnings


def _apply_owned_parts(manuscript: str, parts: Mapping[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """Own only marked part spans; no heading can authorize replacing BODY."""
    result = manuscript
    log = []
    title = str(parts.get("title") or "").strip()
    if title:
        result = re.sub(r"^# .*$", lambda _m: "# " + title, result, count=1, flags=re.MULTILINE)
    for stage, heading in (("abstract", "摘要"), ("introduction", "引言"), ("conclusion", "结语")):
        text = str(parts.get(stage) or "").strip()
        if not text:
            continue
        if stage == "abstract" and parts.get("keywords"):
            text += "\n\n**关键词：** " + "；".join(parts["keywords"])
        start, end = f"<!-- manuscript-part:{stage}:start -->", f"<!-- manuscript-part:{stage}:end -->"
        block = f"{start}\n## {heading}\n\n{text}\n{end}"
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        if start in result or end in result:
            if result.count(start) != 1 or result.count(end) != 1 or not pattern.search(result):
                raise FrontBackError(f"invalid_owned_part_markers:{stage}")
            result = pattern.sub(lambda _m: block, result, count=1)
            position = "owned_part_replaced"
        elif stage == "conclusion":
            result = _insert_before_references(result, block)
            position = "standalone_before_references"
        else:
            abstract_end = "<!-- manuscript-part:abstract:end -->"
            if stage == "introduction" and abstract_end in result:
                at = result.index(abstract_end) + len(abstract_end)
            else:
                title_match = re.search(r"^# .*$", result, re.MULTILINE)
                at = title_match.end() if title_match else 0
            result = result[:at].rstrip() + "\n\n" + block + "\n\n" + result[at:].lstrip("\n")
            position = "standalone_front"
        log.append({"part": stage, "position": position})
    return result, log

# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def run_front_back_stage(
    *,
    draft_path: str | Path,
    research_question: str,
    chapter_roles: Sequence[Mapping[str, Any]],
    out_dir: str | Path,
    parts_fixture_path: str | Path | None = None,
    recordings: Mapping[str, Mapping[str, Any]] | None = None,
    language: str = "zh",
    planning_context: Mapping[str, Any] | None = None,
    client: Any | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    """Serial generation over the current body, then one final assembly.

    Stage N's message is built only after stage N-1's response was parsed,
    so the introduction reads the fresh conclusion and the abstract reads
    both fresh parts.  Parts come from a labeled manual fixture file,
    recorded responses, or an explicitly injected live client.  The live
    route uses the existing module4 ``invoke_client`` protocol and never
    creates a budget ledger of its own.
    """

    if client is not None and (parts_fixture_path is not None or recordings is not None):
        raise FrontBackError("live_client_fixture_or_recordings_conflict")

    draft_path = Path(draft_path).resolve()
    out_dir = Path(out_dir).resolve()
    manuscript = draft_path.read_text(encoding="utf-8")
    if len(manuscript) > FULL_TEXT_SINGLE_PASS_CHAR_LIMIT:
        raise FrontBackError(
            f"manuscript_too_long_for_single_pass:{len(manuscript)}:"
            "chapter_wise_reading_not_implemented")

    context = normalize_planning_context(planning_context)
    unsupported = _unsupported_placements(context) if context is not None else []
    if unsupported:
        report = {"stage": "front_back", "status": "unsupported_placement",
                  "planning_context": context, "placement_execution": unsupported,
                  "generated": [], "failures": unsupported, "application_log": [],
                  "source_draft": str(draft_path), "final_manuscript": "",
                  "model_calls": 0, "successful_model_calls": 0,
                  "model_attempts": 0, "model_call_records": [],
                  "external_requests": 0}
        _write_json(out_dir / "FRONT_BACK_REPORT.json", report)
        return report

    conflicts, placement_warnings = (
        _placement_conflicts(manuscript, chapter_roles, context)
        if context is not None else ([], []))
    if conflicts:
        report = {"stage": "front_back", "status": "placement_conflict",
                  "planning_context": context, "conflicts": conflicts,
                  "placement_execution": "blocked", "placement_warnings": placement_warnings,
                  "generated": [], "failures": conflicts, "application_log": [],
                  "source_draft": str(draft_path), "final_manuscript": "",
                  "model_calls": 0, "successful_model_calls": 0,
                  "model_attempts": 0, "model_call_records": [],
                  "external_requests": 0}
        _write_json(out_dir / "FRONT_BACK_REPORT.json", report)
        return report

    from .module4.runtime import invoke_client
    from .review_delivery import ReplayClient, MissingRecording

    fixture = None
    replay = None
    parts_source = ""
    live = client is not None
    if live:
        parts_source = "injected_live_client"
    elif parts_fixture_path:
        fixture = json.loads(Path(parts_fixture_path).read_text(encoding="utf-8"))
        parts_source = "labeled_manual_fixture:" + str(parts_fixture_path)
    else:
        replay = ReplayClient(recordings or {})
        parts_source = "recording"

    messages_dir = out_dir / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    parts: dict[str, Any] = {}
    generated_stages: list[str] = []
    missing_stages: list[str] = []
    failures: list[dict[str, str]] = []
    model_calls = 0
    successful_model_calls = 0
    model_attempts = 0
    model_call_records: list[dict[str, Any]] = []

    client_kwargs: dict[str, Any] = {}
    if model is not None:
        client_kwargs["model"] = model
    if max_output_tokens is not None:
        client_kwargs["max_output_tokens"] = int(max_output_tokens)
    if thinking_budget is not None:
        client_kwargs["thinking_budget"] = int(thinking_budget)

    def _live_record(stage: str, response: Any = None, error: BaseException | None = None) -> dict[str, Any]:
        """Keep client telemetry while excluding content and raw response bytes."""

        source: Mapping[str, Any] = response if isinstance(response, Mapping) else {}
        if not source and error is not None:
            candidate = getattr(error, "record", None)
            if isinstance(candidate, Mapping):
                source = candidate
        record: dict[str, Any] = {"stage": stage}
        for key in ("call_id", "requested_model", "returned_model", "finish_reason",
                    "complete", "request_id", "raw_response_sha256", "status_code",
                    "key_index"):
            if key in source:
                record[key] = source[key]
        try:
            attempts = max(1, int(source.get("attempt") or 1))
        except (TypeError, ValueError):
            attempts = 1
        record["attempts"] = attempts
        usage = source.get("usage")
        if isinstance(usage, Mapping):
            record["usage"] = dict(usage)
        if error is not None:
            record["error_type"] = type(error).__name__
        return record

    def _live_complete(response: Any) -> bool:
        return (isinstance(response, Mapping)
                and response.get("complete") is True
                and response.get("finish_reason") == "stop")

    for stage in STAGE_ORDER:
        if fixture is not None and stage not in fixture:
            missing_stages.append(stage)
            failures.append({"stage": stage, "error": "part_not_provided"})
            continue
        # Serial dependency: this stage's message carries the parts already
        # PARSED in this run (conclusion -> introduction -> abstract).
        messages = build_stage_messages(
            stage, body_text=manuscript, research_question=research_question,
            chapter_roles=chapter_roles, prior_parts=parts, language=language,
            planning_context=context)
        _write_json(messages_dir / f"front_back_{stage}_messages.json", messages)
        try:
            if fixture is not None:
                response = fixture[stage]
            elif live:
                model_calls += 1
                response = invoke_client(
                    client, messages, call_id=f"front_back:{stage}", **client_kwargs)
                record = _live_record(stage, response)
                model_call_records.append(record)
                model_attempts += int(record["attempts"])
                if _live_complete(response):
                    successful_model_calls += 1
                if not _live_complete(response):
                    raise FrontBackError(
                        f"front_back_incomplete:{stage}:"
                        f"finish_reason={record.get('finish_reason')!r}")
            else:
                key = f"front_back:{stage}"
                replay.next(key, messages)
                response = replay(messages)
            parts.update(parse_front_back_response(stage, response))
            generated_stages.append(stage)
        except MissingRecording:
            missing_stages.append(stage)
            failures.append({"stage": stage, "error": "pending_missing_recording"})
        except FrontBackError as exc:
            missing_stages.append(stage)
            failures.append({"stage": stage, "error": str(exc)})
            if live:
                # A later part must never be built from an unparsed or
                # incomplete predecessor.  Fixture/replay compatibility
                # intentionally keeps its historical continue behavior.
                break
        except Exception as exc:
            if live:
                record = _live_record(stage, error=exc)
                model_call_records.append(record)
                model_attempts += int(record["attempts"])
                missing_stages.append(stage)
                failures.append({"stage": stage,
                                 "error": f"front_back_client_error:{type(exc).__name__}"})
                break
            raise

    final_text = manuscript
    application_log: list[dict[str, str]] = []
    if parts:
        final_text, application_log = apply_front_back(
            manuscript, parts, chapter_roles, planning_context=context)
    final_path = out_dir / "MANUSCRIPT_FINAL.md"
    final_path.write_text(final_text, encoding="utf-8", newline="\n")
    if not parts:
        status = "no_parts_generated"
    elif failures:
        status = "partial"
    else:
        status = "generated"
    report = {
        "stage": "front_back",
        "status": status,
        "parts_source": parts_source,
        "contract_mode": "manuscript_parts_plan" if context is not None else "legacy_fixture",
        "planning_context": context,
        "planning_context_source": ("final_planning_artifact" if context and "status" in context
                                    else "explicit_compact_fixture" if context else "legacy"),
        "placement_execution": "standalone" if context is not None else "legacy",
        "placement_warnings": placement_warnings,
        "generated": generated_stages,
        "missing_stages": missing_stages,
        "failures": failures,
        "application_log": application_log,
        "source_draft": str(draft_path),
        "final_manuscript": str(final_path),
        "model_calls": model_calls,
        "successful_model_calls": successful_model_calls,
        "model_attempts": model_attempts,
        "model_call_records": model_call_records,
        "external_requests": model_attempts,
    }
    _write_json(out_dir / "FRONT_BACK_REPORT.json", report)
    return report


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
