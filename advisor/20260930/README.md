# 外部 GPT-6 Pro 顾问包：OptoMind Review v2（2026-09-30）

这是一个只读审阅包，供外部 GPT-6 Pro 评估完整综述的投稿级质量、现有 30 篇综述范例的结构启发，以及“导师式审稿意见 → 局部替换建议”的下一步设计。包内路径均相对于本目录。

先读：

1. [ADVISOR_PROMPT.md](ADVISOR_PROMPT.md)：本次顾问任务、边界、交付格式和成本实验要求。
2. `current/docs/REVIEW_V2_FROZEN_BASELINE.md`：冻结基线与已知限制。
3. `current/docs/REVIEW_V2_DELIVERY_STATUS_20260930.md`：最新交付层实现与 61 项通过的验收摘要。
4. `delivery/real_manuscript/REVIEW_DRAFT.md`：2026-09-27 的真实六章正文。
5. `delivery/offline_fixture_integration/main.pdf` 与同目录 Markdown：离线集成交付稿；其中首尾部件、编辑替换和图为明确标注的 fixture。
6. `exemplars/study/REVIEW_STRUCTURE_FINDINGS.md`：30 篇范例的七类写作用途、结构比较与限制。
7. `feedback/LOCAL_AI_FEEDBACK.txt`：另一个 AI 的意见原文；文本中还含有用户转贴的外部评语，来源身份以文本自身为准。

## 稿件身份

`delivery/real_manuscript/` 是真实 9 月 27 日写作运行的六章正文。它不是 9 月 30 日冻结链重新生成的整篇稿件；其 `FRONT_MATTER.md`/`BACK_MATTER.md` 以及后续人工首尾整理可能与最终交付稿不同。

`delivery/offline_fixture_integration/` 是工程交付链的离线集成样本。`configs/HISTORY_EDIT_FIXTURE.json`、`HISTORY_PARTS_FIXTURE.json` 和 `HISTORY_DELIVERY_CONFIG.json` 明确记录了 mock 编辑与首尾输入；不要把它的摘要、引言、结语或示意图当作模型质量证据。

`delivery/content_handoff_03_real/` 是冻结版内容交接的真实小测，包含根代理评语以及 C6_U01–U03 的正文、输入和实际消息。

## 30 篇综述范例

完整身份清单见 `exemplars/manifest.json`，结构研究入口见 `exemplars/study/INDEX.md`。`exemplars/records/` 有 30 个 ID：

- 27 个 `local_fulltext_verified`：每个目录提供 `structure.json`（机器可读全文、段落/章节结构与来源定位）及 `READING_VIEW.md`（人类可读全文/阅读视图）。
- P03、P06、P09：只有 `ONLINE_ONLY.md`、DOI 和已核验来源 URL；没有本地全文，不能称为 30 篇都已下载。

原始 27 篇 PDF/XML/HTML 没有重复复制，因为机器友好结构已经包含全文。这样可以让顾问完整比较内容，同时避免把材料缓存、失败响应和排除的 Perspective 带入包中。`exemplars/study/ROOT_COLLECTION_AUDIT.json`、`ROOT_SPOTCHECKS.json` 和 `READING_NOTES.md` 提供采集审计、人工抽查与逐篇结构结论。

## 当前实现与证据

`current/implementation/` 保留交付入口、全文编辑器、首尾部件、引用/图表装配、全文汇编和相关运行时构造器。`current/prompts/` 包含实际提示资产；较长的通用规划和写作指令仍由 `progressive_review_plan.py`、`chapter_arrangement.py` 与 `review_unit_writer.py` 在运行时构造。`current/tests/upgrade3/` 只选取与交付、内容交接和消费路径直接相关的测试。

`delivery/offline_fixture_integration/reports/`、`delivery/real_manuscript/RUN_REPORT.md` 与 `current/docs/REVIEW_V2_DELIVERY_STATUS_20260930.md` 是工程结果证据。它们证明交付链和离线出版样例的行为，不证明整篇科学稿件已经投稿级或事实零错误。

## 资产边界

本包不包含凭据、密钥、缓存、数据库、无关运行树、pycache、整目录账本或 Git 元数据。包内的“real”表示来源于真实本地写作/交接运行；“offline_fixture”表示明确记录的离线模拟输入；“online-only”表示保留身份但未下载全文。

文件来源和复制映射见 `PACK_MANIFEST.json`。它用于识别材料版本，不是科学质量验收。

## 可以直接打开的材料

- [真实六章全文](delivery/real_manuscript/REVIEW_DRAFT.md) / [句柄稿](delivery/real_manuscript/REVIEW_DRAFT_HANDLES.md)
- [46页集成PDF](delivery/offline_fixture_integration/main.pdf) / [集成Markdown](delivery/offline_fixture_integration/MANUSCRIPT_PUBLISHED.md)（fixture首尾与示意图）
- [30篇范例索引](exemplars/study/INDEX.md) / [结构研究结论](exemplars/study/REVIEW_STRUCTURE_FINDINGS.md) / [逐篇笔记](exemplars/study/READING_NOTES.md)
- [另一位AI意见](feedback/LOCAL_AI_FEEDBACK.txt)
- [冻结真实小测的根代理意见](delivery/content_handoff_03_real/ASTRA_REVIEW.md)
- [当前交付层状态](current/docs/REVIEW_V2_DELIVERY_STATUS_20260930.md)
- [ZIP下载](../OptoMind_advisor_pack_20260930.zip)

核心代码直接使用仓库根目录的最新实现，而非把包内快照当作另一个版本：[统一入口](../../optomind_research/runtime/upgrade3/review_delivery.py)、[全文编辑](../../optomind_research/runtime/upgrade3/article_text_editor.py)、[首尾](../../optomind_research/runtime/upgrade3/manuscript_front_back.py)、[图表引用](../../optomind_research/runtime/upgrade3/delivery_citations.py)、[出版器](../../optomind_research/runtime/latex_publication_renderer.py)。GitHub看不全时切换Raw视图。

补充：[受限计划报告](delivery/restricted_plan_reports/PLAN_DELIVERY_REPORT.json)保留原计划4、已装配3及缺件，不将处理结束视为原计划完整。包内current/README与AGENTS保留工作区背景，其中禁推规则由本次用户明确发布授权覆盖；顾问阅读云端不需要执行这些本地管理操作。
