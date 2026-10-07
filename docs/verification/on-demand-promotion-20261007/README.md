# 按需 Max 正式接入：离线交付与本地验收入口

## 本轮决定

将轻导航、自主选材、完整回读和分步预算作为**显式启用的细纲加强模块**的默认按需引擎。第一层决定是否及哪些同章任务簇值得投入；原 BODY planner 的阶段顺序和科学提示词保持。当前工作是把已有加强结果可靠送入完整章节、编排和写作。

基线：`57848a4aaf89d5b11a34f37cfaf81ce49c067f20`。对应真实 ACCESS/OWNER 运行源码为 `00a91359fd50ddcc26355c94c8bb067c25985f6a`；后续导航身份分组修复为 `4604451df04b7ce9896fe28c5fd8410493fb9cb8`。不要把三个版本混为一次模型实验。

## 正式输入输出流

完整 planner 输出目录（`DETAILED_REVIEW_PLAN.json` 与其 `writer_packets` 清单）
→ 第一层完整细纲筛选，当前轻导航为辅助
→ 按稳定任务 ID 投影同章任务簇，其他职责只读
→ 现有 Plus 访问器 / 本地搜索、展开 / 完整记录回读
→ 现有按需 Max 修订和材料采纳
→ 将可编辑单元合回最新完整章节，保留其他章节和未选单元
→ 导出完整 `DETAILED_REVIEW_PLAN.json` + `writer_packets/<chapter>.json`
→ 现有正式 `chapter_arrangement.py`
→ 现有正式 `review_unit_writer.py`。

新增入口 `scripts/upgrade3/outline_strengthening_pipeline.py` 只负责串接现有模块、落盘与恢复，没有另一套规划算法。旧 `outline_strengthening.py --mode on_demand` 仍是单任务簇入口，它的 `ARRANGEMENT_PACKET.json` 只代表选中任务簇，不能当作完整章节。

## 这次修了什么

1. **完整章节与稳定身份。** 统一 `unit_id_remap` 为新 ID → 原 ID 列表；拆分按负责人返回顺序，合并放到最早原位置；不改未选单元。跨次修订保留当前 ID 到原始 ID 的映射，更新可编辑 ID 和身份合同。后续只读参照跟随当前拆合结果，移除已被替代的旧职责；真正外部背景仍保留。
2. **实际消费者。** `case_objects`、完整 supporting study 记录、独立 `limits`、`synthesis_and_transition` 到达编排和 writer 的 owner context。段落任务与 argument_relations 继续保留。字段转换不要求模型重新从 A/B 猜回负责人已经安排的关系。
3. **材料采纳。** 有内容和身份的嵌套综述转述来源可以进入当前采纳与写作；同身份互补 A/B、精读、补充和片段保留完整记录。当前池明确更新的更正快照继续覆盖旧版本，避免把已修正旧事实重新当作互补材料。输入超限分批时，每个成功批次累积采纳材料，最终不再用初始池覆盖。
4. **正式执行与恢复。** 按需 CLI 默认流式，请求超时 1800 秒、流总超时 3600 秒；沿用原成功配置中的模型和额度。prepare 核对 ACCESS/OWNER 提示、完整 profile、计量依据和执行配置。恢复时用已保存 raw 重新做当前版本的材料采纳和结构检查，不重复收费；传输参数变化后沿用已完成答案时，报告原执行设置，避免把缓存当作新设置实际执行。
5. **有界重新尝试。** 预算或网络中断后正常恢复；对已经保存但结构未解决的回答，显式 `--retry-unresolved` 归档对应失败 stage/raw 后重试一次，保留成功 ACCESS 和已接纳组。没有自动付费循环。
6. **记录纠正。** 历史 ROOT_REVIEW 的 Plus 字符数改为 292,983、两笔结算为 0.094140 / 2.409984 元，与 ACTUAL_COMPARISON 和账本一致；总费仍为 2.504124 元。

## 运行方式

使用实际完整规划目录，不用公开脱敏消息直接冒充生产材料池。先离线准备：

```bash
python scripts/upgrade3/outline_strengthening_pipeline.py --packet-root <完整规划目录> --output <新的加强运行目录>
```

本地得到真实调用授权后，以同一路径运行，预算上限应与既有共享账本的授权总上限一致：

```bash
python scripts/upgrade3/outline_strengthening_pipeline.py --packet-root <完整规划目录> --output <新的加强运行目录> --run --budget-ledger <共享账本> --budget-limit <已授权总上限> --key-file <本地密钥文件> --tokenizer <本地分词器>
```

默认 ACCESS=`autonomous_outline`、OWNER=`strong_outline`，均通过现有 quality profile 解析。按需阶段默认开启思考 32,768，回答容量 32,768，实际 wire 为 `max_completion_tokens=65,536`；最终以该运行实际 request/effective_request 为准。第一层使用既有 `outline_selection` 配置。模型、任务或材料变化需要重新核对实际请求，不按名称猜配置。

中断恢复重复同一命令。结果确属已保存的 unresolved/invalid，查明后才附加 `--retry-unresolved`。原始输入已经改变，应换新输出目录保留旧成果；同一输入的兼容阶段缓存会复用。不要删除共享账本或把旧费用归零。

## 阶段文件与消费者

- `PIPELINE_INPUT.json`：完整输入指纹、来源目录和执行源码哈希
- `run_sources/`：各次所用源码清单；不能只用 Git HEAD 掩盖未提交工作区差异
- `selection/SELECTION_REQUEST.json`、`SELECTION_RESULT.json`：第一层真实合同和结果
- `groups/<group_id>/INPUT.json`、`REQUEST.json`：从当前完整章节投影的输入与实际配置
- `groups/<group_id>/stages/`：ACCESS、OWNER、补读及分批 checkpoint/raw；兼容恢复与失败历史在这里
- `groups/<group_id>/RESULT.json`：已验证的局部结果，不作为整章输入
- `revisions/<revision>/DETAILED_REVIEW_PLAN.json`、`writer_packets/`：可直接交给正式编排 CLI 的完整快照
- `CURRENT_PLAN.json`：明确指向已接纳快照；失败重试不会将之前的有效指针退回原始粗稿
- `PIPELINE_REPORT.json`：本次状态、分组状态、候选与实际接纳目录。partial 时优先读 `adopted_packet_root` 和 CURRENT_PLAN；不能猜“最近目录”

正式编排仍单独启动，使用 `CURRENT_PLAN.json` 的 packet_root：

```bash
python scripts/upgrade3/chapter_arrangement.py --packet-root <CURRENT_PLAN中的packet_root> --chapter <章节ID> --output-root <新编排目录>
```

默认仅预览消息。真实编排与写作由本地另行授权，保持原 CLI 的预算/参数要求；本轮入口不自动生成正文。

## 已有真实回答的离线重放

`test_outline_promotion_pipeline.py::test_recorded_owner_return_through_formal_pipeline_and_arranger_writer` 使用原档案的真实 OWNER 解析结果和公开的 18 份脱敏材料，通过正式入口完成合回、正式编排 CLI 重导出和 writer CLI 消息预览。selector、ACCESS 和编排响应是明确标记的受控结果；额外一个未选单元也是结构测试夹具。它验证消费者接线，不冒充原 367 份本地池或原完整第二章。

`DOWNSTREAM_GUIDANCE.json` 保存输入、修前修后消息哈希和字段核对：两种写作模式中，10 个案例 finding、18 条研究限制和两段综合衔接完整进入实际消息；原 7 个任务、论证关系和完整 A/B 保持。

完整未选单元保留、拆合、跨章只读更新、失败恢复另有跨领域结构测试。完整原 Ch2 未选单元细纲和原私有 367 份材料不在公开包中，本地正式测试请沿生产完整 packet 构造，不用云端夹具替换。

## 本地下一步最小验收

1. 在完整真实规划的副本上，复用已保存的选择/负责人返回验证完整章节合回；核对未选单元逐字段相同，正确拆合及最新只读职责
2. 在最终 adopted packet 上跑一次局部真实编排与写作；检查条件、对比对象、案例职责、反例与综合衔接是否实际展开
3. 预算不足时停在 ACCESS 后，恢复后确认不再次支付选材费用；核对请求真实 stream、timeout 和额度
4. 满意后再决定扩大任务簇覆盖。无需再跑 Plus/Max 模型竞赛或整篇 BODY

离线验证命令、失败集合与源码范围见 `VERIFICATION.json`。原始材料与密钥留本地；科学提示词没有写入本题答案。
