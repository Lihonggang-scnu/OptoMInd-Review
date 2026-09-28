"""Versioned prompts for the local research-chat interface.

The prompts in this module are intentionally separate from the web server so
that users can inspect exactly what is sent to Qwen. They request structured
decisions and concise rationales, never hidden chain-of-thought.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Mapping


PROMPT_VERSION = "research-chat-v5-first-principles"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def build_retrieval_intent_prompt(question: str) -> str:
    return f"""你是光学薄膜科研任务规划员。只分析用户究竟要解决什么物理问题，并制定检索计划；此阶段禁止先写答案，防止用已有印象绑架后续检索。

用户问题：
{question}

请返回一个 JSON 对象，字段如下：
{{
  "normalized_question": "不改变原意的规范化问题",
  "answer_type": "spectral_target | design_principle | literature_review | clarification_needed",
  "primary_entity": "主要器件、材料体系或应用对象",
  "application": "应用场景",
  "physical_problem": {{
    "system_boundary": "被设计薄膜、基底、外部环境和受益对象的边界",
    "incident_inputs": ["进入系统的辐射、角度、偏振、环境或工况"],
    "desired_outcomes": ["用户最终想改变的物理或应用结果"],
    "controllable_spectral_properties": ["T | R | A | emissivity 中可设计的量"],
    "causal_chain": ["光谱响应如何影响中间物理量并最终影响应用结果"],
    "objective_quantity": "最终应优化或约束的物理量",
    "physical_constraints": ["守恒关系、本征截止、环境窗口等不可忽略约束"],
    "operating_environment": {{
      "angle": "已知值或 unknown",
      "polarization": "已知值或 unknown",
      "temperature": "已知值或 unknown",
      "other": []
    }}
  }},
  "default_assumptions": ["用户未说明时采用的必要默认假设"],
  "ambiguities": ["会显著改变答案、需要用户确认的信息"],
  "applicability_dimensions": [
    "证据能否迁移时必须一致的维度，如对象、材料体系、器件结构、环境、指标定义和测试条件"
  ],
  "must_match": ["候选文献必须满足的主题条件，使用英文短语"],
  "must_exclude": ["应排除的错域材料、器件或研究主题，使用英文短语"],
  "required_answer_fields": ["最终回答必须直接给出的内容"],
  "required_claims": [
    {{
      "claim_id": "C001",
      "question": "最终回答必须解决的一个原子问题",
      "claim_type": "physical_principle | target_band | numeric_target | weighting_method | benchmark | constraint",
      "required_evidence": "physical_law | direct_numeric | direct_qualitative | benchmark | user_confirmation",
      "confirmation_required": true
    }}
  ],
  "queries": [
    {{
      "query": "精确英文检索式",
      "purpose": "该检索式要回答哪个证据问题",
      "claim_ids": ["C001"],
      "evidence_level": "direct_numeric | benchmark | review | mechanism",
      "min_year": 2019
    }}
  ]
}}

规则：
1. 从第一性原理拆分：进入系统的辐射是什么，可控光谱量是什么，中间物理机制是什么，最终应用结果是什么。
2. 不允许把相关性当因果性。必须写出“光谱属性 -> 中间物理量 -> 最终结果”的因果链，并标记缺失环节。
3. 证据适用性至少考虑对象、材料/器件结构、环境、指标定义、测试条件五个维度；关键词相同不能替代这些维度一致。
4. 每个 required_claim 必须是可独立验证的原子主张。数值目标、波段、权重方法和物理原理分开登记。
5. 用户问光谱目标时，检索式优先寻找波段、T/R/A/emissivity、阈值、权重函数和应用结果之间的定量关系，而不是泛泛材料论文。
6. 至少生成直接定量、综述/基准、机理三类检索式。默认 min_year 为 2019；基础定律可设 min_year=0。
7. 不要声称已经联网，不要编造论文，不要输出回答正文，只返回 JSON。"""


def build_source_review_prompt(
    question: str,
    retrieval_intent: Mapping[str, Any],
    sources: Iterable[Mapping[str, Any]],
) -> str:
    compact_sources = []
    for source in sources:
        compact_sources.append(
            {
                "source_id": source.get("source_id"),
                "title": source.get("title"),
                "year": source.get("year"),
                "backend": source.get("backend"),
                "journal_or_venue": source.get("journal_or_venue"),
                "doi": source.get("doi"),
                "abstract_or_snippet": str(
                    source.get("abstract_or_snippet", ""),
                )[:1000],
            },
        )
    return f"""你是严格的文献相关性评审员，不负责搜索，也不负责写最终答案。

用户问题：
{question}

检索意图：
{_json(dict(retrieval_intent))}

候选文献：
{_json(compact_sources)}

对每篇文献返回：
{{
  "source_reviews": [
    {{
      "source_id": "必须来自候选文献",
      "relevance": "direct | supporting | background_only | wrong_domain",
      "reason": "一句话说明它与用户问题的实体、应用和所需回答是否匹配",
      "evidence_potential": "direct_numeric | benchmark | directional | none",
      "applicability_alignment": "exact | partial | mismatch",
      "applicability_notes": ["对象、结构、环境、指标定义、测试条件的匹配情况"],
      "supports_claim_ids": ["只能引用 retrieval_intent.required_claims 中的 claim_id"],
      "reported_values": [
        {{
          "quantity": "文献实际报告的物理量",
          "value": "数值或范围",
          "unit": "单位",
          "conditions": "结构、环境、波段、角度、测量/仿真条件"
        }}
      ],
      "supported_answer_fields": ["它能支持哪些最终回答字段"],
      "conflict_with_intent": ["与 must_exclude 或默认假设冲突之处"],
      "recommended_action": "use_for_answer | use_as_background | need_full_text | reject"
    }}
  ],
  "coverage": {{
    "direct_numeric_count": 0,
    "claim_coverage": [
      {{
        "claim_id": "C001",
        "status": "direct | partial | benchmark_only | unsupported",
        "source_ids": [],
        "reason": "为什么达到或未达到所需证据等级"
      }}
    ],
    "missing_answer_fields": [],
    "enough_for_answer": false,
    "reason": "简短说明"
  }}
}}

严格规则：
1. 题名或摘要只出现相同关键词，不等于支持主张。
2. 必须逐项核对 applicability_dimensions；关键维度不一致时不得标为 exact。
3. 只有文本直接报告了与 claim_id 对应的数值、单位和条件，才能标为 direct_numeric。
4. benchmark 是特定条件下的案例结果，不能自动成为通用目标。
5. mechanism/directional 只能支持因果方向，不能支持目标数值。
6. supporting/background_only 文献不能替代直接证据。
7. 不得“宽松纳入”来凑数量，不得编造文本中不存在的信息，只返回 JSON。"""


def build_answer_prompt(
    question: str,
    retrieval_intent: Mapping[str, Any],
    direct_sources: Iterable[Mapping[str, Any]],
    background_sources: Iterable[Mapping[str, Any]],
    evidence_coverage: Mapping[str, Any] | None = None,
) -> str:
    def compact(source: Mapping[str, Any]) -> Dict[str, Any]:
        review = source.get("source_review", {})
        return {
            "source_id": source.get("source_id"),
            "title": source.get("title"),
            "authors": list(source.get("authors", []))[:4],
            "year": source.get("year"),
            "backend": source.get("backend"),
            "journal_or_venue": source.get("journal_or_venue"),
            "doi": source.get("doi"),
            "abstract_or_snippet": str(
                source.get("abstract_or_snippet", ""),
            )[:1400],
            "source_review": review,
        }

    direct = [compact(source) for source in direct_sources]
    background = [compact(source) for source in background_sources]
    coverage = dict(evidence_coverage or {})
    return f"""你是光学薄膜科研助手。请直接回答用户的问题，但必须先构造可审计的主张证据账本。只返回一个 JSON 对象。

用户问题：
{question}

任务理解：
{_json(dict(retrieval_intent))}

证据覆盖审查：
{_json(coverage)}

可用于回答的文献。必须根据 source_review 区分：
- direct_numeric：可支持对应的定量主张；
- benchmark：只能表述为该论文中的案例基准，不能外推为行业通用阈值；
- directional：只能支持方向性判断。
{_json(direct)}

仅可作为背景、不得支撑定量结论的文献：
{_json(background)}

输出结构：
{{
  "answer_markdown": "面向用户的完整中文回答",
  "proposed_status": "confirmed | draft | insufficient",
  "claim_ledger": [
    {{
      "claim_id": "对应 required_claims 的 claim_id，补充主张可用 A001",
      "statement": "回答中实际出现的原子主张",
      "claim_type": "user_constraint | physical_principle | literature_numeric | literature_directional | benchmark | engineering_assumption",
      "numeric_value": null,
      "unit": "",
      "source_ids": [],
      "applicability_scope": "该主张成立的对象、结构、环境和条件",
      "evidence_strength": "direct | partial | benchmark_only | none",
      "needs_human_review": true
    }}
  ],
  "unresolved_claim_ids": [],
  "human_review_questions": []
}}

回答与账本规则：
1. 第一段直接回答。光谱目标优先给出波段、属性、目标类型、数值/范围或可计算目标函数。
2. 每个关键句必须在 claim_ledger 中有对应原子主张。
3. literature_numeric 必须绑定 applicability_alignment=exact 且 evidence_potential=direct_numeric 的来源。
4. benchmark 只能表述为特定条件下的案例结果，不能外推为通用目标。
5. physical_principle 必须说明适用边界；物理定律不能自动给出工程阈值。
6. engineering_assumption 必须 needs_human_review=true，不能伪装成文献结论。
7. required_claims 未达到 required_evidence 时，必须列入 unresolved_claim_ids。
8. proposed_status=confirmed 仅适用于所有需确认主张均达到证据要求或由用户明确给定的情况。
9. 可以给出确定性目标生成器所需的分段建议，但不要生成连续光谱数组，也不要进入膜系结构或优化算法。
10. 只引用给定 source_id；正文中的 [1][2] 编号按文献列表顺序。
11. 用中文回答；首次出现英文术语时附中文解释。"""


def build_critique_prompt(
    question: str,
    retrieval_intent: Mapping[str, Any],
    answer_envelope: Mapping[str, Any],
    direct_sources: Iterable[Mapping[str, Any]],
    evidence_coverage: Mapping[str, Any] | None = None,
) -> str:
    source_ids = [source.get("source_id") for source in direct_sources]
    return f"""你是严格的回答质量审查员。只返回 JSON。

用户问题：
{question}

任务理解：
{_json(dict(retrieval_intent))}

证据覆盖审查：
{_json(dict(evidence_coverage or {}))}

允许支撑核心结论的 source_id：
{_json(source_ids)}

待审回答及主张证据账本：
{_json(dict(answer_envelope))}

返回：
{{
  "score": 1,
  "direct": false,
  "off_topic": "none | slight | significant",
  "unsupported_numeric_claims": [],
  "wrong_domain_citations": [],
  "claim_ledger_issues": [],
  "missing_answer_fields": [],
  "must_revise": true,
  "revision_instructions": ["可执行的修改要求"],
  "honesty": 1
}}

若回答主要在复述文献、没有直接回答用户问题，必须 must_revise=true。
若账本中的证据等级高于来源实际等级、适用范围被扩大、数值主张缺直接证据或 proposed_status 过高，必须 must_revise=true。"""


def build_revision_prompt(
    question: str,
    original_answer: str,
    critique: Mapping[str, Any],
    answer_prompt: str,
) -> str:
    return f"""{answer_prompt}

上一版回答 JSON：
{original_answer[:9000]}

质量审查结果：
{_json(dict(critique))}

请按审查意见重写完整 JSON 对象。不要解释修改过程。"""


def prompt_catalog() -> Dict[str, Any]:
    """Return inspectable prompt metadata without user/source payloads."""
    return {
        "prompt_version": PROMPT_VERSION,
        "pipeline": [
            "retrieval_intent",
            "deterministic_retrieval",
            "strict_source_review",
            "claim_ledger_synthesis",
            "quality_critique",
            "conditional_revision",
        ],
        "principles": [
            "先理解和检索，后写答案",
            "先定义系统边界、因果链和待证明主张",
            "证据必须绑定原子主张和适用范围",
            "案例基准不得外推为通用目标",
            "确定性工具决定确认、草案或证据不足",
            "不生成连续光谱数组",
        ],
    }
