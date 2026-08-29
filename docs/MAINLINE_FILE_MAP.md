# Mainline file map

This is the release-facing map of the active review pipeline. It names the
stable entry points and the key helper modules; it is not a list of every test
fixture or historical experiment.

| Stage | Responsibility | Main source paths | Main outputs |
| --- | --- | --- | --- |
| 1. 启动与配置 | Assemble the question, model policy, provider configuration, and runtime parameters. | `run_review_harness.py`; `config/`; `domain_config.yaml`; `prompts/`; `schemas/` | Run directory, configuration snapshot, cost preflight |
| 2. 理解用户问题 | Turn a natural-language request into a normalized topic, scope, and section plan. | `optomind_research/query_planner.py`; `optomind_research/runtime/topic_identity.py` | `query_planner/`, `TOPIC_IDENTITY.json`, entry gate |
| 3. 找资料 | Discover papers, acquire open full text, canonicalize it, and retrieve traceable chunks. | `optomind_research/s2_discovery.py`; `optomind_research/s2_fulltext_acquisition.py`; `optomind_research/s2_text_chunk_retriever.py`; supporting `s2_*` modules | `s2_literature_intelligence/`, material flow and source ledgers |
| 4. 中央材料库 | Reuse processed material locally and build a topic-scoped knowledge base. | `optomind_research/runtime/central_material_cache.py`; `optomind_research/runtime/topic_scoped_kb_stage.py`; `optomind_research/s2_cache.py` | `long_term_material_cache_sync/`, `topic_scoped_kb/` |
| 5. 按章节整理证据 | Match chapter needs to candidate evidence and select a grounded portfolio. | `optomind_research/runtime/section_coverage_orchestrator.py`; `optomind_research/runtime/evidence_packet_parser.py`; `optomind_research/runtime/evidence_portfolio_selector.py`; `optomind_research/section_evidence_handles.py` | `section_coverage/`, section source ledgers and coverage gates |
| 6. 形成论点 | Convert evidence into claim cards and a production handoff. | `optomind_research/runtime/phase3_argument_orchestrator.py`; `optomind_research/runtime/r3_production_handoff.py`; `optomind_research/runtime/r4_phase3_artifacts.py`; `optomind_research/claim_evidence_binder.py` | `phase3_argument_orchestration/`, R3/R4 acceptance artifacts |
| 7. 写各章节初稿 | Author each section from its evidence-bound argument cards. | `optomind_research/runtime/compact_section_authoring.py`; `optomind_research/runtime/section_authoring_assets.py`; `prompts/` | `authoring/`, section candidates, citation maps |
| 8. 章节资产加强器 | Add bounded explanatory context, representative applications, and explanatory citations. | `optomind_research/chapter_asset_enhancer.py`; `scripts/run_chapter_asset_enhancer.py` | `publication_mainline/enhancement/` |
| 9. 全文交接与全文司令员 | Combine chapters, remove collisions, and enforce whole-manuscript structure. | `optomind_research/runtime/full_manuscript_handoff.py`; `optomind_research/runtime/global_manuscript_commander.py` | `publication_mainline/handoff/`, commander state and manuscript manifest |
| 10. 标题、结论、引言、摘要 | Complete the article around the established body instead of using the user question as the title. | `optomind_research/runtime/staged_article_completion.py`; `optomind_research/runtime/article_completion_runner.py` | `publication_mainline/staged_completion/` |
| 11. 视觉规划与图片挂载 | Plan, audit, procure, generate, and mount visual assets with evidence alignment. | `optomind_research/runtime/visual_editor_runner.py`; `optomind_research/runtime/article_visual_asset_planner.py`; `optomind_research/runtime/visual_evidence_factory.py`; `optomind_research/runtime/visual_procurement_pipeline.py` | `visual_editor/`, visual contract, package and audit |
| 12. 最终引用信息补全 | Resolve author/year/journal/DOI and related metadata through local and online providers. | `optomind_research/runtime/publication_metadata_resolver.py`; `optomind_research/runtime/openalex_metadata_provider.py`; Crossref/Semantic Scholar adapters under `tools/academic_backends/` | `publication/metadata/`, bibliography and metadata audits |
| 13. 生成 PDF | Compile and audit the English and Chinese publication packages. | `optomind_research/runtime/latex_publication_renderer.py`; `optomind_research/runtime/bilingual_publication.py`; `scripts/run_bilingual_publication.py` | `publication/latex/`, `publication/latex_zh/`, PDF and integrity reports |

## Default policy

The quick path uses `private_study`, keeps the central material cache and
evidence gates active, disables the optional research-plan branch when
`--no-research-plan` is supplied, and leaves the expensive global Phase 3 DAG
off unless `--phase3-llm-dag` is explicitly requested. Chapter style governance
and visual processing follow the selected execution profile and record their
own reports rather than silently changing the scientific evidence.

## What belongs in a source upload

The source upload should contain the modules above, their tests, configuration
templates, prompts, the static replay, and the portable `artifacts/e2e/`
publication layer after review. It should not contain private credentials,
local caches, downloaded source-paper trees, virtual environments, native
build output, or the separate TMM line.
