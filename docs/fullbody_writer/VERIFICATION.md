# 全文写作候选：实现与离线验收

2026-10-07，基于 `lihonggang-dev` 的 `80180d9c6fc975e9dfa3430adcfd3a89b361697a`。本轮新增独立的完整 BODY 写作候选，不重跑上游或云端付费模型。实际研究与源码依据见 [RESEARCH_BASIS.md](RESEARCH_BASIS.md)。

## 实际交接检查

- 三章跨领域受控输入经过真实编排入口、材料消费者、完整清单、四条写作路线、解析、缓存和文件落盘；只替换响应边界，不把永远成功的上层 fake 当整链验收
- 原始用户要求、读者定位、完整任务、条件、论证关系、案例与材料末尾内容实际进入消息
- 连续路线下一次请求包含前次真实正文；工作台按 ID 取回完整原文和材料，不拿导航冒充科学证据，已经驻留的内容不再次重复发送
- 独立读者只看到完整稿与原始读者目标；编辑角色收到相应完整证据，精确补丁之外的字节保持；同一问题可修改多个相关位置
- 相关位置需要新增材料时显式补入并记录额外调用。个别读者位置失效只保留该问题待处理，其他有效编辑仍执行，不重新付费阅读整篇
- 正常恢复零新调用；底稿仅运行编号变化时不再重复调用读者/编辑；未知尝试和旧有效正文保留；已知输出触顶支持显式原文前缀续写
- 使用真实 Qwen 请求序列化、流解析与 SQLite 账本，验证各角色模型、思考/回答额度、流式开关和超时；HTTP/凭据边界使用受控替代，没有连接真实模型

## 真实历史资产检查

1. 公开的 Ch1、Ch2 历史任务与材料通过实际输入构造器重建；旧 Ch2 成稿在恢复引号格式后逐字保持。这些是历史资产消费验证
2. 原 BODY40 七章计划、七份 packet、编排和视图离线加载，保留 **7章、29单元、91任务、288份精确材料记录、222个统一来源身份**。226个历史私有定位路径在云端不可用，明确记录，已有 inline 材料继续参与
3. 查出并修复跨章身份接缝：某篇论文在旧章节采用规范身份，在另一章作为显式别名。仅在明确别名连接和稳定身份一致时统一导航，完整材料记录及引用不改写；矛盾身份仍拒绝
4. 七章整体输入紧凑序列化为3,418,070字节，旧章级投影合计3,321,270字节，新增约2.91%；科研材料池保持完整，没有嵌入49MB历史planner状态。单章试算中最初23%的重复包装已消除
5. 云端没有本地 tokenizer，七章预览使用UTF-8字节上界，whole_author及continuous_author的首个请求会被该保守估值拦下。这不是实际模型容量结论。本地真实运行必须使用已有 tokenizer 核对后决定路线，不能因此删材料或宣称某候选质量差

详细输入、输出、哈希和复现边界见 [INTEGRATION_EVIDENCE.json](INTEGRATION_EVIDENCE.json)。公开归档不是本地私有原请求逐字节回放，也不作为本轮生产缓存。

## 发现并处理的工程问题

- 跨章 canonical/alias 碰撞，导致合法同篇材料无法汇合
- 原问题/读者字段在 CLI 与模型消息之间的命名接缝
- 同一细纲、真实前文和回读材料重复出现在消息中
- 底稿恢复只改运行编号，却导致读者/编辑再次计费
- 精准修改后仍保留旧片段正文与哈希，后续可能回看错版本
- 相关修改位置超出实际供材范围时，没有补入对应证据
- 可选 Windows 卡片定位在 Linux 上产生路径错误，导致有用 inline 材料连带失败

最后一项对既有 `review_unit_writer.py` 只增加读卡错误转换，沿用原有诊断和 inline 材料保留行为；没有改写旧 BODY 提示词、规划顺序、案例或编排算法。

## 测试命令与结果

```bash
python -m pytest -q tests/upgrade3/test_fullbody_contracts.py tests/upgrade3/test_fullbody_engine.py tests/upgrade3/test_fullbody_cli.py tests/upgrade3/test_fullbody_integration.py
python -m pytest -q tests/upgrade3
python -m compileall -q optomind_research/runtime/upgrade3/fullbody_contracts.py optomind_research/runtime/upgrade3/fullbody_writer.py optomind_research/runtime/upgrade3/review_unit_writer.py scripts/upgrade3/fullbody_writer.py
 git diff --check
```

最终结果：新增全文专项 **122通过**；包含既有写作/传输的有界回归 **271通过、10个subtests通过**；完整 `tests/upgrade3` **1738通过、17失败、10个subtests通过**。17项失败集合与修改前逐项相同。`compileall`、`git diff --check` 通过。源码哈希与命令由同目录 `TEST_RESULTS.json` 记录。修改前全套基线为1616通过、17失败、10个subtests通过。既有17项分别是5项旧材料目录字段约定失败、11项私有历史产物缺失、1项固定Windows路径；本轮不修改这些历史测试或伪造缺失资产。

## 本地接下来验证什么

用最新完整批准细纲、实际材料与tokenizer运行四条路线的适用路径，共用新增100元预算。重点亲读完整正文的章节推进、机制解释、比较、条件与阴性结果，以及后半篇是否充分展开。先检查实际消息与执行，再判断方案本身。第四条复用已有完整稿，额外阅读、修订及补材调用都计费。

云端没有新的付费生成。完整真实写作和质量比较由本地按开工指令执行；首尾模块与全文出版装配不在这一轮候选比较中。
