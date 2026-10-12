# OptoMind 完整稿校验、引用编号与出版交接核对


核对基线：Lihonggang-scnu/OptoMInd-Review，提交 2a28854cf86a98f5afb4a6e5fbe1a925694fd138。所有源码、事实与链接固定于该提交；2026-10-12 只读研究。没有修改生产代码、稿件、BODY 作者、首尾提示词或其他研究成果，没有调用项目付费 API。


## 结论先行


1. 新全文校验员在本提交中尚未实现。20261012 准备目录确实存在，不是缺失目录：完整 Git tree 共 9,818 项且 truncated=false；该目录含 README、盘点/集成建议、通用提示词草案及云端调研任务。两轮方案是设计，不是已经跑通的模块。[准备状态与两轮解释](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/manuscript-checker-preparation-20261012/README.md#L3-L17)。

2. 最小接线点是现有 03 首尾装配成功之后、04 编号投影之前。保持 BODY 原有 article_text_editor 不动，新阶段只接收实际完整句柄稿并返回明确选中的稿件；04 只消费该返回值，05 继续只消费 04 的最终读者稿。当前两条真实调用路径都没有这个新阶段。[协调器当前接缝](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L951-L999)；[本次真实运行的04/05消费者](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/run_final_delivery.py#L19-L45)。

3. “先句柄稿编辑，再唯一映射编号”符合当前最终交付架构。但整个历史不是只发生过一次编号：BODY 编辑后已有 BODY 读者稿；03 使用选中 BODY 的句柄稿；完整稿在 04 重新按最终出现顺序生成唯一映射。[BODY选用与编号](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L1056-L1082)；[04唯一映射](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/delivery_citations.py#L430-L485)。

4. 本次独立离线复核全部通过：31 份源码/归档的 Git blob 与固定树逐一匹配；03 前后 BODY 投影逐字一致；去旧书目投影可重现 04 输入；188 个句柄归并为 187 个引用身份，可逐字重建 04 读者稿；05 出版 Markdown 与 04 读者稿字节相同。证明的是交接一致性，不能证明科学质量合格。详见本包 handoff_verification.json。

5. 已有编辑器与新校验员有术语、重复、标题、衔接上的职责重叠，适合复用补丁底座，不宜原样再调用旧 run_text_edit_stage：它绑定旧提示词/单轮流程、live 模式自带编号、存在跳过即 pending 的选择语义，并未实现新两轮失败反馈。[旧任务提示词](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/prompts/article_text_editor.md#L1-L35)；[live阶段行为](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L263-L370)。


## 1. 版本和资料的可信边界


- 固定提交完整树中存在 docs/acceptance/manuscript-checker-preparation-20261012/，目录 tree SHA 为 cfc4d8622ca9e095ef51db38cbb935f0bc5db968。没有为解决路径问题切换到默认分支或最新提交。[固定目录README](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/manuscript-checker-preparation-20261012/README.md)。

- CLOUD_READ_FIRST 明确记载新校验员未实现、未运行；准备文档再次区分用户方向与本地具体设计，尤其“首次全文检查＋一次失败反馈”的两次总调用解释。[对齐入口](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/CLOUD_READ_FIRST.md#L26-L32)；[草案定位](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/manuscript-checker-preparation-20261012/README.md#L7-L15)。

- 源码的当前行为也支持这个区分：run_downstream_delivery 在 r3 成功门槛后直接进入 identity_catalogs 合并、numbering_input、04 调用；归档 run_final_delivery.py 也直接从 REPLAY_REPORT.final_manuscript 到 04，再从 04 报告到 05。不能把草案写成当前生产步骤。

- 历史真实运行的版本/工作区归属应按归档保留：运行开始是 7a18d0e… 加工作区修改，完成后源码锁定 508cf724…，本次审计读取的是包含源码及归档的 2a28854…。归档 Windows 绝对路径只是原运行证据，不表示云端有这些文件。[运行版本说明](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/CLOUD_READ_FIRST.md#L5-L11)。


## 2. 已实现链路与真实消费顺序


### 2.1 BODY 已选稿


BODY 原有全文局部编辑发生在单元写作、质量门槛和装配完成之后，输入 assembled/REVIEW_DRAFT_HANDLES.md，真实 consumer 位于 legacy_unit_route.py 630–660。它把 run_text_edit_stage 返回的 article 保存到 report[article_edit]。[BODY编辑调用](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/legacy_unit_route.py#L630-L663)。

run_review_delivery(start="body") 随后执行 _body_delivery_artifacts：仅在 article.status 为 edited/no_change 且有 edited_draft 时选该稿，source=article_edit；否则默认装配稿，再由 body_delivery_state 检查是否满足配置要求。请求了 article_edit 时，编辑未完成/选中来源不符会构成 blocker，不能把回退到装配稿误说成编辑成功。[选用逻辑](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L1056-L1127)；[正式入口](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L1174-L1199)。

本次归档确实选择 article_edit/c270e6…/CANDIDATE_HANDLES.md，selected_body.status=complete，source=article_edit，BODY 29/29 单元生成完成，body_delivery.ready=true；旧总状态 restricted_draft 与 complete_with_diagnostics 同时存在，表达的是保留诊断的交付状态。[实际selected_body和body_delivery](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/input/BODY_DELIVERY_REPORT.json#L12426-L12460)。

### 2.2 03 首尾生成与装配


在统一下游 BODY 分支，02 只返回 body_selected 和 selected_body.handles_draft，不再重复调用旧编辑器。03 接收 r2.edited_draft，post_body 模式调用 run_serial_parts。[02复用和03消费](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L883-L945)。

独立 CLI scripts/upgrade3/manuscript_parts.py 的职责只到首尾：live adapter 验证传入 draft 路径等于 BODY 报告实际 handles_draft、body_delivery.ready=true，未显式提供目录时读取 selected_body.references_path。它不能被写成自动重跑所有上游的总 CLI。[独立CLI边界](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/scripts/upgrade3/manuscript_parts.py#L1-L18)；[live输入校验](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/serial_parts_live.py#L63-L82)。

run_serial_parts 读取不可变原稿/真实 BODY，四阶段顺序生成，调用 apply_serial_parts 后再次比较 extract_body；仅完整成功时原子落盘 MANUSCRIPT_FINAL.md 并返回 generated、final_manuscript、body_preserved=true、source_identity_map。[输入与身份](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/serial_manuscript_parts.py#L366-L414)；[生成、原子选择与失败](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/serial_manuscript_parts.py#L423-L483)。

serial_parts_application 管理 title/abstract/introduction/conclusion 的 ownership 标记，仅替换拥有的 span、允许的 article title，结语插在旧书目之前；不提供任意 BODY 局部编辑权限。它是首尾装配安全边界，不应拿来充当新全文局部编辑执行器。[模块合同](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/serial_parts_application.py#L1-L15)；[装配与BODY校验](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/serial_parts_application.py#L379-L448)。

本次真实选中的是 03_front_back/attempts/attempt_003/MANUSCRIPT_FINAL.md。attempt_003 是恢复/装配尝试编号，不代表新校验员第三轮，也不是审校轮次证据。报告中的 source_body_delivery 继续携带诊断。[实际被选完整稿](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/REPLAY_REPORT.json#L185-L247)；[live选择返回值](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/serial_parts_live.py#L198-L218)。

### 2.3 从完整句柄稿到最终引用编号


统一协调器先检查 r3.status=generated 且 final_manuscript 文件存在。随后 _post_body_delivery_catalogs 合并既有目录与 03 验证过的 source_identity_map。身份上下文对 bare/namespace alias 做一致性验证；同名、无 DOI/paper_id 的跨 namespace 不明身份会报错。[身份校验与合并](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L698-L800)；[03到04门槛](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L951-L982)。

_post_body_numbering_input 仅从不归首尾所有的区段去除旧书目，逐字保留四类 owned parts，并记录 source_draft/source_sha256 和投影路径/hash；它保留原 03 稿，防止旧参考文献本身进入最终出现顺序，亦避免旧书目正则吞掉首尾标记。新校验应先于该投影，投影也必须消费校验后所选稿。[投影合同和路径](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L1030-L1053)。

04 run_figures_citations_stage 的固定顺序是：图资产进入句柄稿 → 在 figure placeholder 状态转换旧交叉引用 → 按最终位置编号图片 → build_delivery_citation_map → 由同一 map 渲染正文引用和参考文献表。扫描是完整稿的实际阅读顺序，包含摘要已有引用、引言、BODY、表格、图注等。[04实现](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/delivery_citations.py#L430-L485)。

身份键优先 DOI（本函数 strip+lower，不等于任意 DOI 字符串均充分规范化）、再 paper_id、再主 handle。完整 namespaced token 映射保存在 token_details；handle_to_reference 是裸句柄便利视图，在跨 namespace 场景不能取代 token_details。裸句柄首先按精确索引查找，找不到再使用首个目录 namespace。[身份与解析](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/delivery_citations.py#L87-L159)；[唯一编号及完整token输出](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/delivery_citations.py#L163-L204)。

未知句柄不会被吞掉，仍留在文本和报告。04.status 目前由 residual 是否为空决定；这不是科学或全部图资产验收。BODY/post_body 的协调器要求 04.status=complete 且实际 reader_draft 存在才出版。[未知token保留](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/delivery_citations.py#L213-L245)；[阶段状态](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/delivery_citations.py#L495-L514)；[出版前门槛](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L987-L999)。

### 2.4 05 最终出版所选稿


05 不重新接图、不重建引用映射、不另选 BODY/03 稿；它接收 04.report 中的 reader_draft、references_path、mapping_path、assets_dir。先核验参考文献段唯一、编号条数与目录相符、没有 figure placeholder、有题名；随后写 MANUSCRIPT_PUBLISHED.md，再从实际 reader_text 提取题名/摘要/关键词。[出版消费与核验](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L1212-L1271)；[有限核验项目](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L1331-L1350)。

有 ownership 标记时，_extract_front_matter 仅取 title/abstract 拥有区段，避免误取 BODY 内名为 Abstract 的小节。TeX/PDF 从 published_path 构建、禁用 Crossref/S2 enrich。[实际前置信息提取](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L1353-L1394)；[TeX实际输入](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/review_delivery.py#L1285-L1304)。

函数理论上会把末尾空白规范成一个换行，所以不能对任意输入笼统声称字节原封不动；本次真实 04/05 字节确实相等。出版 complete 主要说明请求的出版文件产出，本次 tex_result 仍为 compiled_awaiting_metadata，scientific_acceptance=false。[本次出版状态与诊断](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/05_publication/DELIVERY_REPORT.json#L27-L43)。


## 3. 输入/输出与选择状态清单


下列路径以固定提交 docs/acceptance/serial-parts-live-20261011/ 为归档根；原始 F: 绝对路径保留在对应 JSON，跨机器必须显式映射。


- 已选BODY：[input/SELECTED_BODY_HANDLES.md](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/input/SELECTED_BODY_HANDLES.md)。来自原 article_edit/…/CANDIDATE_HANDLES.md；selected_body.source=article_edit；BODY ready，带科学诊断。

- BODY身份目录：[input/SELECTED_BODY_REFERENCES.json](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/input/SELECTED_BODY_REFERENCES.json)。来自已选BODY的 numbered/REFERENCES.json；句柄身份来源，并非要求后续沿用旧数字。

- 03完整句柄稿：[03_front_back/attempts/attempt_003/MANUSCRIPT_FINAL.md](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/03_front_back/attempts/attempt_003/MANUSCRIPT_FINAL.md)。REPLAY_REPORT.final_manuscript 明确选中；generated，BODY preserved。

- 04编号投影输入：[04_NUMBERING_INPUT_HANDLES.md](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/04_NUMBERING_INPUT_HANDLES.md)。去旧、非owned书目，保留完整实际前后部件；不是编辑器候选稿。

- 04图表/引用后的句柄稿：[04_figures_citations/MANUSCRIPT_HANDLES.md](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/04_figures_citations/MANUSCRIPT_HANDLES.md)。04返回 handles_draft；本次没有图资产。

- 04最终编号读者稿：[04_figures_citations/MANUSCRIPT_READER.md](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/04_figures_citations/MANUSCRIPT_READER.md)。04返回 reader_draft；complete，187引用身份，无未知句柄。

- 04编号身份输出：[04_figures_citations/REFERENCES.json](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/04_figures_citations/REFERENCES.json)。references＋token_details＋handle_to_reference，保留188句柄到187身份的关系。

- 05最终出版Markdown：[05_publication/MANUSCRIPT_PUBLISHED.md](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/05_publication/MANUSCRIPT_PUBLISHED.md)。严格由04实际reader_draft输入；本次字节相同。

- 05出版元数据：[05_publication/PUBLICATION_METADATA.json](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/05_publication/PUBLICATION_METADATA.json)。从04实际reader_text提取，而非生成时旧metadata。

- 05出版报告/PDF：[05_publication/DELIVERY_REPORT.json](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/05_publication/DELIVERY_REPORT.json)。complete；tex/main.pdf已归档；compiled_awaiting_metadata；scientific_acceptance=false。


本次 04 与 05 的报告会由归档 driver 显式追加 source_body_delivery/scientific_acceptance。通用 run_publication_delivery 本身没有 science_diagnostics 输入参数；以后不要假设新校验诊断会自动跨层传递，需在协调报告/sidecar 中接通。[真实driver的诊断搬运](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/serial-parts-live-20261011/run_final_delivery.py#L30-L45)。


## 4. 旧编辑器与新校验员的职责分界


### 已有 article_text_editor


- 当前 BODY consumer 在首尾之前。旧 prompt 关注少量高价值重复、同义术语写法、相邻章节衔接、任务式标题，通常不超过10处；不做新检索、不重写整篇、不做逐单元科学审稿。[旧prompt](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/prompts/article_text_editor.md#L1-L35)。

- build_edit_passes 发出一次全文协调请求，全文上限400,000字符；超长时直接报不支持，而不是已实现跨章协调。该消息只有职责和全文，identity_catalogs 只在 live后处理/身份审计中用，不自动向模型提供新校验所需来源目录或证据包。[一次全文消息](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L37-L68)；[身份后处理](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L344-L360)。

- parser/applier 提供精确目标和原快照区间应用，是可复用基础，但并不代表满足草案全部合同：旧 target_block_id 仅日志，旧日志原文/替换截短120字符，重叠是先遇到者保留、后遇到者跳过；无两轮失败反馈。此处只讨论接线所需的行为差异。[原快照执行器](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L115-L164)。

### 新全文校验（仍是设计）


- 完整阅读 title/abstract/introduction/BODY/conclusion，发现跨首尾的一致性、术语/缩写、数字单位数学、结构/图表/引文、表达与无效重复问题；只能在当前稿与提供证据能支持时局部改正，缺证据列未决，不能接管写作构思、文献检索和科学质量终审。[完整稿校验草案](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/docs/acceptance/manuscript-checker-preparation-20261012/FULL_MANUSCRIPT_CHECKER_PROMPT.txt)。

- 与旧编辑器重叠的术语/标题/重复属于不同时间点的补漏：旧编辑器保持 BODY 原路线，新校验补完整稿新增的错配。避免把旧prompt整套再执行一遍，或另搬完整科学审稿框架。

- 不把 apply_serial_parts 的 body_preserved=true 延伸成“新校验后 BODY 逐字不变”的声明：新校验若允许有证据的局部 BODY 修正，应记具体已批准差分，保持未涉及区段不变。首尾装配的零修改保证与后置局部修改的审计范围应分别记录。


## 5. 最小接线建议（未实施）


建议只添加独立成稿校验 stage/orchestrator 和薄接口，复用审计后的局部替换底座；不改 BODY 作者、原首尾生成模块或原提示词。保留现有04和05的主要职责。


1. 在 review_delivery.py r3 成功门槛之后、_post_body_numbering_input 之前接入。先完成 _post_body_delivery_catalogs 的身份合并，把 actual full handle draft＋正文/提示词/材料/配置身份＋日期/明确刊规提供给新阶段。独立首尾实跑的 run_final_delivery.py 路径也必须接入同一薄函数，否则只改通用协调器不会覆盖已展示的真实消费者。

2. 新阶段返回显式 selected_manuscript（绝对路径＋sha256）、execution_status、selection_state、applied/skipped、unresolved_questions、diagnostics、input_identity、source_identity_changes。字段名属于建议，当前源码没有这个成稿选择合同。无修改应显式选回当前快照；禁止猜测“最新文件”、直接 glob 或偷偷退回原03稿。

3. 同一快照定位、严格JSON、唯一精确原文、互不重叠、管理标记保护、完整失败上下文由确定性底座负责。第二轮仅处理第一轮失败，使用第一轮成功应用后的完整候选稿；成功变更保留。两次总审校调用是准备文档的方案，不是既有代码能力。预算、请求响应、恢复身份沿现有受控客户端/缓存，不新造项目账本。

4. 来源目录身份不可重写；“不可改文献身份”不宜误落实成禁止所有引文位置修正。若确有材料依据，局部引用可改为目录内明确可解析的稳定句柄，并记录旧token→新token及理由；禁止模型自行改最终[N]、编造来源或改变目录所代表论文。对无证据或模糊身份保持未决。

5. 04 的 numbering_input 必须改为新阶段返回的 selected_manuscript，再调用现有 _post_body_numbering_input 和 run_figures_citations_stage；同一最终 map 同时输出 reader citations 与 references。不要复用旧 BODY 数字或让新校验先输出读者编号稿。直接复用旧 live runner 会额外做编号，应在新的薄stage中调用可复用解析/补丁组件，而不是整段嵌套旧阶段。

6. 硬阻断与诊断分开：没有完整有效模型响应/选中稿丢失/快照身份不符/管理标记损坏/不可解析引用，应阻断下游；已完成校验但仍有科学未决或有限替换失败，可按事先定义的策略选“保留成功修改的带诊断稿”，保留原报告，不把发布状态当科学通过。这个可交付策略尚需明确实现，不能借用旧status推断。

7. 05 本身继续只从04提取元数据和出版。新阶段的 source_body_delivery＋checker_diagnostics＋selection provenance 应放在协调报告/明确sidecar并传到最终交付报告；现有 generic publisher不会自动携带它们。

8. 04 当前会在校验之后新插图注；若将来使用含引用的 figure_assets，新校验前看不到最终插入的图注。首版可显式提供这些待插入图注作为材料，并在04后做确定性残余/映射检查；这属于边界说明，不值得为本次无图稿迁移整个出版阶段。[图注插入时点](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/delivery_citations.py#L451-L472)。


## 6. 最低限度交接验收建议


以下是未来实现应加入的离线/fixture验收，不是本次已经跑过的新校验测试：


- 无修改：明确选择当轮完整句柄快照，04/05与未启用校验基线一致。

- 真实局部修正：改变摘要一个唯一片段，04读者稿、05元数据和出版稿必须共同反映新内容，证明没有偷用03旧路径。

- 部分成功＋失败反馈：第二轮输入包含成功应用后的全稿；只处理失败ID；最终选择保留成功修改，未决报告保留。

- 跨稿引用修正：只在指定局部把可解析句柄A换成目录内B，重新最终编号，token_details、正文和书目一致；不得全局替换历史数字。

- 管理标记/缺文件/身份错配：必须硬失败，不能用上次文件或装配稿绕过。

- 旧书目与owned parts：投影去除旧书目，不丢title/abstract/introduction/conclusion标记，尤其结语紧接旧书目时。

- 选中稿溯源：原稿、每轮候选、选中稿、04投影、04读者稿hash可追踪；03的body_preserved证明只保留其原适用范围。

- 诊断传递：BODY complete_with_diagnostics、新校验未决、出版文件状态分别可见，scientific_acceptance不会因完整交付变为true。

- 若将来有图注：最终04图注中的句柄被同一map编号，未知句柄和缺图记录可见，不误称新校验已经读过未提供的图。


## 7. 本次实际执行的验证与附件索引


执行了 verify_handoff.py：只导入三份固定提交的纯本地文本/引用模块，在已获取归档上重建字符串与map；不调用live适配器、API、生产CLI、PDF编译或任何付费模型。所有检查通过：

- 31/31源码与归档 Git blob 精确匹配（source_verification.json）

- 03 BODY字节投影一致，sha256=540d4c76f4e5065601a3bb2787b81577b38e5a7896bd06eb48a86bb3066ac973

- 03完整稿sha256=278f3a383dd6cb62c52111a3960ee51880b414109c5021021afee13e158c93d6

- 04投影sha256=575ed43d9474b1c4dd53df67d6724476f9a71015513106b336b80b10724f77ef

- 188稳定句柄、187canonical引用；reference records与token_details完全匹配；unknown/residual均空

- 04 reader全文逐字重建；05 published字节相等，sha256=8c25f1701db19c04acbebb5a7add2127c2811115d2811f5a8fabb1f575f6c06e


文件：HANDOFF_AUDIT.md（本报告）、verify_handoff.py（离线核对脚本）、handoff_verification.json（核对结果）、source_verification.json（31份副本blob/hash回执）、tree_inventory.json（固定commit完整树）、sources/（精确源码和归档副本）。


本次不复评具体医学主张，不把准备文档列举的科学案例当必须命中的标准答案，也不改动当前稿件。此报告限定为 OptoMind 输入、选用、引用身份和出版交接，不展开外部补丁格式/定位算法选型。
