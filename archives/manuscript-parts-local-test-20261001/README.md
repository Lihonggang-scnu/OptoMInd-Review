# manuscript-parts 本地真实测试归档（2026-10-01）

这是历史测试和问题分析归档，供开发“正文完成后再运行”的独立首尾模块参考。归档任务仅整理已有结果，没有修改算法，没有新模型调用。这里的 PLAN、cache、稿件、职责卡都不是 lihonggang 或任何新运行的默认生产输入。

## 优先阅读

1. `curated/code/PROVENANCE.json`：实际测试分支、最终代码版本、未提交差异及无法确认的逐调用版本。
2. `raw_public/body_restore_20261001/BODY_RESTORE_ACCEPTANCE_REPORT.md`：最终协调者验收，优先于早期乐观报告。
3. `raw_public/body_restore_20261001/ROOT_BODY_PLUS_READ_REVIEW.md`：11条材料核对及具体问题。
4. `raw_public/body_restore_20261001/parts_tuned/MANUSCRIPT_FINAL.md`：最后真实完整稿；`body_plus/assembly/REVIEW_DRAFT_HANDLES.md` 是同轮正文。
5. `raw_public/body_restore_20261001/MANUSCRIPT_PARTS_EVOLUTION.json`：实际 v0/v1/v2 职责卡。
6. `manifest.json`、`curated/PUBLICATION_AND_OMISSIONS.md`：文件用途、原文件与公开副本指纹、删节、仅在本地的材料索引。

## 测试代码究竟是什么

实际测试分支为 `review-v2-manuscript-parts-local-acceptance`，最终提交为 `32d7349158283eb52bdcc232a38c28fc700412f0`，归档开始时跟踪文件及非忽略未跟踪状态为空。它是在远端原版本 `ad173f9677cc2e4c0f765b7cd923d3546b1d1943` 上继续本地修补的结果，不能把全部测试归为 ad173f9。

17个本地修补提交保留为本归档分支的祖先。补丁、提交时间和说明见 `curated/code/`。历史运行跨越多个提交及 resume；没有逐请求保存确切 Git SHA 的阶段明确记为未确认，不能把最终 SHA 倒填为所有调用的代码版本。实际 messages 和 raw response 是对应调用输入输出的更直接记录。

最终版本的本地验收脚本也纳入归档。这些脚本有运行过程中修改，最终文件字节不保证等同于每次历史启动时字节；以运行报告、日志、实际消息为准。它们是验收接线，不是新增生产入口。

归档分支从实际测试提交新建，不改写历史，不修改 main / review-v2 / review-v2-manuscript-parts / lihonggang，不合并。根 README 中旧的冻结/后续待办只用于理解历史；本归档不授权重新开发或真实测试。

## 实际跑到哪里

先前运行完成规划及正文，但正文材料覆盖明显变薄；随后恢复 BODY 顺序、修补材料/cache/引用等接缝，再完成真实规划、5章20个单元的正文与两轮真实串行首尾生成。

- `new_plan` 与较早正文：前轮被压薄结果，约71篇实际去重来源，保留为失败分析依据。
- `body_restore_20261001/plan`：恢复后的完整规划；585行初始卡片池，最终身份目录654项。身份目录数不是实际使用论文数。
- `body_restore_20261001/body`：首轮恢复写作，验收脚本误用 qwen3.7-flash，108篇实际来源，保留原结果。
- `body_restore_20261001/body_plus`：恢复历史实际写作配置 qwen3.5-plus，20/20单元完成，127个引用句柄经归并为125篇，5张表；5个单元存在内容/材料问题，装配完整不等于问题解决。
- `parts` / `parts_tuned`：两轮真实 Conclusion → Introduction → Abstract，各3次调用、finish_reason=stop。最终真实题名、摘要、引言、结语已生成，5章 BODY 保留。没有对最新稿生成或视觉验收投稿PDF。

最终结论是结构分离能运行，正文有恢复，未达到“仅分离轻量首尾而维持旧正文全部优势”的目标，不建议整体合并或冻结为稳定交付。只有一个真实主题，不能宣称跨领域内容质量已经验证。

## 观察结果与原因推测分开

| 实际观察 | 尚不能据此证实的原因 |
| --- | --- |
| 旧稿176篇来源；前轮71篇；恢复 Flash108篇；最终 Plus125篇。 | 不能把差额全部归咎于并行职责卡。模型配置、执行顺序、任务分配和材料使用都改变过。 |
| 最终机制章比旧稿短，互补案例少；临床与干预部分相对较强。 | 知识缩窄可能发生于多个规划/修订/写作阶段，未完成各因素独立实验。 |
| 旧第1章“引言”含深度背景；新BODY未全部承接其知识。 | “引言必须独立”不意味着旧章全部属于轻量开场，也不意味着按标题删除有效。 |
| 后置首尾实际输入充分，仍围绕少数缺口；引言像缩略结论。 | 可能涉及职责卡焦点、提示词和模型综合能力，未证明是单一问题。 |
| Plus稿纠正部分人群/方案框架，保留阴性试验和安全性。 | 较强模型未自动修正所有上游卡片错误，也未保证综合表述不扩大。 |

具体反例见亲审报告：抗生素时间锚点错误与因果扩大；PICRUSt2在章节间称谓冲突；UBA6酶靶点被笼统称为受体轴；特殊工程模型条件省略。未手工修正文科学内容来制造通过。

## 旧版比较来源

旧规划来自本地 `20260926_astra_repair/run585`，旧正文来自 `20260927_run01`（6章23单元、176篇、10表）。这份旧稿摘要/结语有人工作用，不是纯自动首尾成绩。旧运行未保存精确生成代码SHA；a61eccacea890d252b3e68be56fed50eb5e10c00 是可读的旧架构公开快照，不能冒充已证明生成该旧稿的提交。

## 模型、预算与输入

原规划启用 `--planning-revision`；阶段命令和实际参数保存在验收脚本、状态、报告、telemetry 中，记录边界见 `curated/code/COMMANDS_AND_CONFIG_EVIDENCE.md`。各阶段配置不同，不能以一个默认模型概括全部阶段。最终正文 Plus 设置：output_tokens=12000、thinking_budget=4000、并发3；后置首尾 Plus 设置：output_tokens=18000、thinking_budget=8192，串行运行。首尾每次实际输入约24.9万token，包括完整BODY、v2、全局规划信息、654项身份目录及8份选定真实材料。Qwen直连。

`curated/budget/LEDGER_EXPORT.json` 是只读导出的本地账本，不是已对账的供应商账单。已结算49.6441824元，reserved4.4921236元，uncertain0.51844元，100元上限下剩余45.345254元。未知中断调用继续占额，归档未重置或释放账本。

## 公开材料边界

仓库是公开仓库。原始PDF/TEI/HTML、数据库、下载包、论文原文片段、摘要原文及权利不明材料不上传。保留 DOI/论文身份/来源链接和本地路径/指纹索引。论文卡A/B和精读中的模型提炼可以作为研究链衍生材料附录；其中原文引用字段仍删节。

`raw_public/` 是由已有文件生成的公开副本。未删节文件保持字节一致；需要删节的文件保留结构及明确标记，并记录原始文件 SHA-256 与公开副本 SHA-256。没有覆盖任何原始产物。`curated/` 是本次人工整理、索引及预算导出，不冒充历史原始响应。

59–67MB原始全量规划、40–46MB运行状态与整个约2GB运行目录不直接塞进Git。规划按顶层结构/章节拆分，以索引列清完整组成；大原件仅在本地，未另传第三方。公开messages可能因原文删节无法逐字复现原调用；请按删节表解释这一限制，不用它们发起生产调用。

## 未验证和缺失项

- 无逐次请求的完整 Git SHA 记录，旧176篇运行精确SHA未确认。
- embedded/distributed首尾位置未真实测试；最新完整稿未做PDF视觉验收。
- 原始全文、全部缓存、素材索引数据库仅在本地；版权许可逐文献未审定。
- 若某阶段未落盘 actual messages/raw，则标注缺失，不能用重新构造提示词补造实际发送记录。
- 程序回归通过和可解析引用不代表科学结论、跨领域质量或投稿质量通过。

后续独立首尾模块可借鉴这里的真实职责卡、串行消息与正文保持方式，以及首尾不足的反例。请选取所需材料，在新运行中明确构造输入；不要自动使用归档 PLAN/cache。

本归档分支顶层排除了祖先已公开的 advisor/ 顾问包（包括范例全文结构与大型ZIP）；排除清单在 `curated/code/EXCLUDED_INHERITED_ADVISOR_FILES.json`。这不删除或改写原有分支或历史。

部分原始driver报告保持较早的failed状态，后续恢复记录与最终装配结果另存，不回写为成功。重点见 `curated/code/COMMANDS_AND_CONFIG_EVIDENCE.md`。

## 云端开发材料导航

- `curated/materials/PLAN_SECTIONS_INDEX.json` 列出全部拆分规划；`PUBLIC_PLAN_OUTLINE.md` 提供便于阅读的规划概览。
- `curated/materials/AB_DEEP_SUMMARIES.index.json` 指向8份去重后的 A/B、精读及补充提炼；packet 的 `.materials.json` 中 `material_refs[].record_sha256` 对应这些记录键。
- `raw_public/body_restore_20261001/body/arrangement/` 保留5章实际编排输入、响应和导出结果。
- `raw_public/body_restore_20261001/body_plus/writer/` 保留20个真实单元的输入、messages、响应、结果及正文。
- 两轮 `parts/` 与 `parts_tuned/` 均附3组实际消息/响应和完整稿。
- `raw_public/all_qwen_raw/` 追加已落盘的去重模型响应；它们属于历史调用，不是新增运行。
- `curated/materials/source_index.json` 说明原材料去向；`curated/SELECTIVE_PASSAGE_REDACTIONS.json` 记录公开副本中残留的原文片段删节。
- `curated/archive_tooling/` 是本次离线归档工具，不是生产算法。

为尽量支持云端修改，实际消息、模型提炼与响应尽可能保留；大规划按结构拆分并抽出重复材料。材料原文仅删节，不以删节版冒充原调用完整复现。

`curated/complete_plans/INDEX.json` 另外提供3次完整规划的公开结构版本：保留全部顶层字段及数组条目，包含章节、工具结果、writer packets，不用形状摘要代替实际内容。递归 `_archive_fragment` 指向同目录 fragments 文件（路径以归档根为基准）；相同容器只存一份，方便云端程序展开。此版本保留来源身份目录；原文字段仍明确删节。

模型返回补充包使用短文件名，历史运行路径见 `curated/RAW_RESPONSE_PATH_INDEX.json`，避免 Windows 长路径丢件。完整公开规划可用 `curated/archive_tooling/read_public_plan.py --run body_restore_20261001 --output <归档外路径>` 离线展开；旧版使用 `--run old_baseline`。
