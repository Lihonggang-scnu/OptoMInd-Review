# PDF 解析部署与消费者衔接（2026-10-13）

本轮选择原生 JATS/TEI 优先；PDF 使用现有 GROBID/fitz 正文提取，加本地 Docling 全页布局、原图裁切及 qwen3.5-plus 公式/表格识别。不是把所有正文提取都宣称为 Docling。实测部署档为 Docling 2 进程、区域 HTTP 全局 8 并发、外层获取 4 worker；类的保守默认值仍为 1/4，正式运行显式传参。

## 输入 → 输出 → 消费

- 输入：论文稳定身份及元数据、原生 XML 或 PDF；本地 Docling 安装/离线模型配置、区域识别配置；既有项目/账户账本、tokenizer 与安全密钥文件路径。密钥内容不进入快照或公开记录。
- 获取：`scripts.upgrade3.build_local_materials.make_material_acquirer_factory` 只构造一次共享工厂，每个 source-unit 有独立产物目录，共用解析器、识别器、缓存、限流与未知费用停止信号。
- 输出：不可变 snapshot 内 `DOCUMENT_MANIFEST.json`、`DOCUMENT_BLOCKS.jsonl`、`DOCUMENT_ASSETS.json`、`REFERENCES.json`、`READING_VIEW.md`，以及原文件/原图/定位/来源哈希；保留 pending、uncertain、error 和预算停止缺口，不用成功计数掩盖失败。
- 阅读：`PreparedSnapshotProvider` 验证后，由普通阅读、A/B、定向阅读或独立 M4 消费同一 snapshot。识别后的公式和表格及其条件脚注进入真实 messages，不只挂一个文件名。
- 后续补材料：正式 `progressive_review_plan` CLI 使用同一组显式解析参数，把共享工厂注入 `make_planning_supplement_runner`；新 PDF 完成同一路线才送阅读卡。原生 XML 不额外做视觉识别。未传工厂的旧调用保留旧行为，不能假定自动继承上次采集配置。

## 本次实际采用路径

根目录：`F:/OptoMind-Review-2/outputs/e2e_optics_20261012_80cny`。

- 选择记录：`worktree/outputs/optics_e2e/04_local_materials_regional_resume_20261013/RECOVERY_SELECTION_MAP.json`
- 获取检查点及 A/B manifest：上级目录的 `selected/`。
- A/B 消费目录：`worktree/outputs/optics_e2e/05_ab_cards_docling_20261013`。
- 规划池目标：`worktree/outputs/optics_e2e/06_planning_pool_docling_20261013/PLANNING_POOL.jsonl`；由正式卡片和 bridge 绑定 card_path、snapshot、PLAN，不直接把短 B 导出冒充规划池。
- 完整实参及免费短链：`worktree/outputs/planning_parser_wiring_final/RESULT.json`；运行绑定在提交后生成新的 V3，V2及原始绑定保留，不覆盖。

270 条获取记录：151 fulltext、100 abstract、19 metadata_only。区域状态 400 recognized、23 uncertain、14 error、0 pending；后两类仍随来源传到阅读器。19 个纯元数据来源不发生付费阅读。两次本地 Docling CLI 失败保留原 PDF/fitz 回退及失败记录；未假定是显存故障。

## 恢复与费用

同一输出、cache、配置和账本恢复；成功区域不重识别，旧快照不覆盖。未知费用原占额保留，已显式审查的旧失败可隔离，新的未知仍停止共享派发。未发送工作恢复，不重下全部 PDF。

A/B 两份内容完整但格式拒收的响应，用显式 saved-response 接口免费重解析。原 INPUT/PROMPT/RAW/USAGE/FAILURE 字节不变，派生卡和绑定回执另存。明确“无直接贡献”的空用途行不变成正向证据，原使用边界保留。已成功卡复用，未调用的暂停 attempt 与实际付费调用分别记录。

本轮修复仅针对解析/消费者/格式恢复接口，不加入光学科学答案，不改变规划和阅读提示词。

## A/B 顶层字段兼容补充

阅读返回明确的 `A` / `B` 时，validator v3 分别映射到正式 `general_understanding` / `review_planning`；两者并存须逐值一致，否则拒绝。原 RAW、提示词、来源与科学内容不修改，诊断只保存映射、原始定位及哈希，不再次复制整份材料。已保存的完整格式失败沿同一严格免费重解析入口恢复；普通调用中的别名直接规范化。两份真实副本恢复与重复复用均为0次调用，原状态和双账不变。


## Supplement-only boundary recovery (2026-10-13)

See [source role and recovery contract](SUPPLEMENT_SCOPE_RECOVERY_20261013.md). Supplementary content stays available to A/B and directed readers, with an explicit main-article gap; original snapshots and successful messages stay immutable. Native saved-response reparsing also handles the exact guarded `approology` field alias without another model call.
