# 完整 BODY 写作候选

四条高级路线与三条朴素基线均显式可选，共用批准后的完整细纲与材料，不改上游规划、案例或编排算法。正式入口：`scripts/upgrade3/fullbody_writer.py`。

## 先读什么

1. [当前本地开工指令：本轮合计40元](PLAIN_BASELINES_40_CNY.md)：新增三条朴素基线、复用底稿、共享预算、具体命令与验收；取代旧100元预算
2. [原四路线背景交接](LOCAL_AGENT_HANDOFF_100_CNY.md)：高级路线的背景和历史说明，旧预算及全部重跑建议不再适用
3. [实现与提示词合同](IMPLEMENTATION_AND_PROMPT_CONTRACT.md)：高级路线如何落实、各字段由谁消费
4. [研究依据](RESEARCH_BASIS.md)：跨领域项目源码、提示词研究及方案来源
5. [朴素基线离线验证](PLAIN_VERIFICATION.md)：本轮真实接缝、预算和回归；[原高级路线验证](VERIFICATION.md) 保留原轮范围

历史单章 `writer_candidates.py` 保留；它的结果只能说明章内表现。这里全部路线的产品和比较单位都是已批准范围内的完整 BODY，首尾模块另行处理。

## 四个可测试候选

| 路线 | 实际工作 | 调用成本 |
|---|---|---|
| `whole_author` 全文受托作者 | 原始问题、全文细纲和相关完整材料一次交给作者，直接生成全部正文 | 正常一次；容量不足在请求前报告，不剪材料或偷偷换方案 |
| `continuous_author` 连续作者 | 按自然章节或完整任务边界推进，每次读真实已写前文、全文原细纲及当前完整材料 | 每个实际写作窗口一次；默认没有最终统稿调用 |
| `workbench` 长文工作台 | 全文原细纲、当前完整材料、最近真实正文常驻；远处正文及批准来源可按身份完整回读 | 作者窗口及回读后的实际继续调用均计费；本地取文件不收费 |
| `reader_revision` 读者驱动修订 | 复用一份完整底稿；新上下文读者发现真实理解问题，作者依据材料做精确修改 | 读者一次＋实际修订批次；必要的补材调用单独记录，底稿不重复生成 |

## 三条朴素基线

| 路线 | 实际工作 | 调用成本 |
|---|---|---|
| `plain_whole` 普通全文写作 | 短的普通学术写作指令＋相同完整细纲和完整材料，一次完成全部 BODY | 正常一次；容量不够明确阻断，不裁材料或缩成小稿 |
| `chapter_concat` 独立章节拼接 | 各章依据完整原细纲及当前完整材料独立写作，不读任何已写前文；必要时只因容量按完整任务组分开，按批准顺序确定性拼接 | 通常每章一次，容量分组如实计次；拼接无模型调用，无隐藏统稿 |
| `hierarchical_full` 独立章节后全文统稿 | 复用同样的独立完整章节底稿，再让编辑器读取完整原稿、全篇计划与全部来源材料，返回完整替换 BODY | 未复用时独立章节/必要容量组调用＋统稿一次；`--draft` 复用 `chapter_concat` 后只付统稿增量 |

三条路线共用 `plain_writer.md`，不继承高级路线的长写作提示、读者诊断或工作台机制。分章基线依然以全部批准 BODY 为最终交付单位，不是一章评测。统稿容量不足或失败时保留完整拼接稿，但 `hierarchical_full` 未完成，不能把它当作统稿成功。

正文输出采用自然 Markdown，任务完成声明放在末尾伴随数据中，不按任务数量强制拆段或小标题。JSON 输出也兼容，已实现历史引号格式的离线恢复。

## 输入与正式接缝

输入清单显式指定全篇章序、各章最终编排、对应 packet root 与可选的确定材料视图。可用 `--plan` 指向批准计划或 `CURRENT_PLAN.json`。`--prepare-manifest` 自动记录文件和哈希；后续预览及真实运行消费保存的清单，避免目录更新后误读另一份材料。具体示例见开工指令。

- 复用真实 `build_unit_view` 获取完整任务、A/B、精读、补充、案例组织、条件和来源用途
- 完整全文纲要保留；当前任务的重复包装以精确引用代替，不重复塞入同一份长材料
- 跨章任务使用稳定全局身份，保留原章节、单元和任务 ID；有明确冲突的论文身份不能合并
- 综述转述、互补材料和显式 alias 正常参与，不强制原始研究具有自己的 A/B 或全文
- 原用户要求、范围和目标读者直达作者与独立读者；独立读者不接收作者自评或人工科学答案
- 正式调用复用当前 Qwen 流式传输与共享账本；900秒无活动超时和3600秒整体时限分别生效

## 模型与预算

默认 `config/fullbody_writer/plus_first.json`，作者、读者、修订均用 Plus，16,384思考＋49,152回答。另有 `economy_flash.json`、`plus_reasoning.json`、`selective_max.json`。Max只在显式配置且带 `--allow-max` 时调用，不自动升级。

用户已将**同一轮全文实验的总预算降为40 CNY**，高级路线和朴素基线合计，已花、失败、预留和不确定占额均计入，不是再追加40元。继续使用本轮同一个账本；已有100元上限用 `scripts/upgrade3/lower_budget_limit.py` 显式降到40并保留全部记录，详见当前开工指令。不能另建40元账本绕过本轮已有费用。`--budget-limit` 始终是绝对累计上限。

优先复用已完成高级稿作为比较材料，只补最有信息价值的基线：容量允许的 `plain_whole`，随后 `chapter_concat`，再 `--draft` 复用它做 `hierarchical_full`。预算不足就报告边界，不要求全路线、全配置组合重跑。默认模型与四份配置保持不变；Max 仍须显式许可。

## 输出与恢复

- `FULL_BODY.md` / `FULL_BODY_RESULT.json`：当前选用的完整或部分稿及完成、来源、任务记录
- `INDEPENDENT_FULL_BODY.md` / `INDEPENDENT_FULL_BODY_RESULT.json`：`chapter_concat` / `hierarchical_full` 留存的独立章节底稿；完成的该底稿可由 `--draft` 复用，不代表统稿已完成
- `RUN_MANIFEST.json` / `IMPLEMENTATION_REPORT.json`：实际阶段、模型、参数、版本选择、恢复与费用归属
- `SOURCE_MANIFEST.json` / `FULL_BODY_INPUT.json`：明确上游版本与完整输入，输入文件位于结果所指的内容寻址目录
- 每次调用保存 `MESSAGES.json`、请求配置、`RAW_RESPONSE.json`、用量及解析结果；各次运行目录不覆盖历史

同输入正常恢复复用成功阶段；变更后按真实消息、有效参数和代码依赖重新判断。解析中断可从已存 raw 离线恢复。新尝试失败时保留旧有效稿，同时明确当前尝试与选用稿的区别。

`continuous_author` / `workbench` 的已知用量输出触顶可通过 `--continue-incomplete` 继续当前真实前缀；三条朴素基线会明确拒绝此选项，避免偷换为连续作者。网络未知结果需要先核对账本，再显式 `--retry-failed`。二者不是同一种恢复，不自动进行付费重试循环。

## 最小离线命令

```bash
python scripts/upgrade3/fullbody_writer.py --help
python -m pytest -q tests/upgrade3/test_fullbody_contracts.py tests/upgrade3/test_fullbody_engine.py tests/upgrade3/test_fullbody_cli.py tests/upgrade3/test_fullbody_integration.py tests/upgrade3/test_fullbody_plain_routes.py tests/upgrade3/test_fullbody_plain_cli.py tests/upgrade3/test_fullbody_plain_integration.py tests/upgrade3/test_lower_budget_limit.py
```

默认不带 `--run` 只预览。`--responses` 在模型边界重放保存结果，仍走真实输入、解析、缓存及落盘路径；不会调用供应商。实际全文风格、展开和费用由本地以同一完整细纲执行本轮测试后比较。
