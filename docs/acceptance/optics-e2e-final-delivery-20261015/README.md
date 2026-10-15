# 光学端到端完整稿与运行记录（2026-10-15）

最终整稿：五章22单元、首尾、90篇去重文献、31页PDF。最终受测源码 `cef2650c5ad48690e2100ec160603b79e63bdf1e`；本目录提交是验收归档，不能混称新模型生成。

优先读 [根最终审阅](c37_publication/root_review/ROOT_FINAL_DELIVERY_REVIEW.md)、[最终稿](c37_publication/MANUSCRIPT_PUBLISHED.md) 和 [来源未使用审计](evidence/unused_source_audit/UNUSED_SOURCE_AUDIT.md)。实际相对路径以 RESTORE_INDEX 记录为准。前期五章编排与第三章修复另见已发布的 `661201fb89a5ccb233412aef4ab8d10c4808495a` 下 `docs/acceptance/optics-e2e-20261014`。

最终 checker 是两份历史付费响应的免费恢复（0新增模型调用）；其前面的首尾4次与checker2次均保留实际参数、完整返回content和费用。完整请求若含来源材料仅留本地定位及哈希，未公开论文全文。

## Staging provenance and restore map

This directory is the local staging area for the small public BODY acceptance package. It is separate from the older `public_body_v33_staging_20261015` directory. The current build is materialized from the ready native BODY report and is still unsubmitted.

The package is pinned to source commit `0b42997aea7f53cef060aa357d125acd455c0d08` and B34 binding SHA `d69ba6ce7c6fc854b97e306b20f8fa514bdea2fb6119f46c96c047c028da79a1`. The builder accepts the native `DELIVERY_REPORT.json` when it reports `body_delivery.ready=true`, `generation_complete=true`, all 22 units complete, `selected_body.status=complete`, and an edited or complete article-editor result. The native `assembly_complete` field is retained as a diagnostic and is not silently promoted.

Run `BUILD_PUBLIC_BODY_V34_STAGING.py` only after that gate. The builder writes only this staging directory. It projects each unit's author body, actual QUALITY_BODY, and model content/usage/effective request/elapsed plus task, identity, and parameter metadata. Full requests, raw responses, SSE streams, UNIT_MESSAGES, input bodies, source materials, credentials, ledgers, PDFs, and XML remain addressable only through local pointers and SHA256 values.

The staging package also records the native assembled draft and selected reader body, article-editor response/execution projection, Root review files for CH1–CH5 (with V32/V33 prior-phase lineage retained for CH1–CH4), the U1 adjacent-string repair proof, the free Codex-capacity recovery proof, the two free unused-source audit records, the four generated post-BODY parts with content/usage/request projections, their generated manuscript, the four-part Root review receipts, and the Root 90-identity reference-coverage audit. The post-BODY calls are marked `new_generation` and the BODY remains hash-preserved; free recovery is recorded separately. Source `0b42997…` is local-only; the historical 661 phase package is recorded separately as already pushed.

`RESTORE_INDEX.json` is marked `body_delivery_ready_staged` and records a content-addressed, unsubmitted restore map. The package is not submitted or pushed by this workflow.


## C37 checker/publication addendum

This refresh adds the final C37 saved-response checker and publication projection from <C37>, pinned to source SHA cef2650c5ad48690e2100ec160603b79e63bdf1e. The two checker responses are preserved as free saved-response recovery evidence: their provider content, usage, effective request, task/identity/parameter projection, and format-recovery record are included; there were 0 new model calls or external requests.

The package includes the final checker report, numbering/reference report and maps, publication delivery/selection metadata, MANUSCRIPT_PUBLISHED.md, and the public generated main.pdf (31 pages). The final reference result is 90 numbered entries, 88 with DOI, and 0 duplicate canonical entries. Full messages/context, provider envelopes/SSE, credentials, ledgers, original paper PDFs/XML, and TeX build intermediates remain excluded. Root's final delivery review is copied under c37_publication/root_review, with a content-addressed pointer.

The same root_review directory contains a projection-only UIR-032/033/034 issue record. It omits the full issue backlog state snapshot, execution state, ledgers, credentials, and database contents.
