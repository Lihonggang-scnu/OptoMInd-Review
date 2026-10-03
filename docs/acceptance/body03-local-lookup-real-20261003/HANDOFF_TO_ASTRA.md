# R2 本地验收交接

当前验收者：**GPT 6.1 sol**。文件名与状态沿用原工单，Astra 不是当前模型名称。

状态：`READY_FOR_ASTRA_03_LOCAL_LOOKUP_R2`

- 基线：`da8c6d3f9a3c079e14ad497318bafc780a955335`
- 独立分支：`body03-local-lookup-cloud-r2-20261003`
- **实现、测试及分项记录提交：`3c1f4ac14f92d5b9b7655bf32deda4f4e335c152`**
- 实测代码树：`1cb2b2e01fe18f1d513f213def44cfc1baca45b2`
- 此交接文件由后续纯文档提交加入；其父提交就是上述实现 SHA。最终分支 tip 在云端完成消息中提供。

## 判断与实际修复

两项要求有益，属于材料进入判断器之前的确定性损失。没有将真实模型输出不理想全部归咎于选材，也没有依据身份命中宣布质量通过。

1. 扩大阅读独立保留原问题前十二篇候选；read_focus 只补剩余候选槽位或已选论文的相关段落。原有材料保留，仍只扩大一次。
2. 指定论文的正文选段保留经过现有过滤的词法命中，再加少量重排互补段；同一正文类型可读取不同章节。同时带入已有 A 的实质认识、B 的相关贡献和有界上下文。新增上下文与段落共用原24K增量额度。
3. 科学判断依旧由原判断器负责，不强制 fulfilled。普通首读、共享 search 默认行为、partial 与失败重试均保留。

详细记录：[开工核对](records/00.md)、[原问题候选保护](records/01.md)、[指定论文多渠道供材](records/02.md)。

## 修改前后实际请求

所有证据均来自合成材料上的生产 SQLite/FTS、适配器和消息构造器，只替换模型及网络边界；不是原始真实论文请求、科学质量验收或原数据库重放。

- [候选漂移：前](records/evidence/baseline/divergent_focus.json) / [后](records/evidence/repaired/divergent_focus.json)：前后首轮输入相同；旧扩大请求只补调查类候选，修复后实际收到原问题的循环涂层观察，且进入 writer material
- [工程多章节论文：前](records/evidence/baseline/package_engineering.json) / [后](records/evidence/repaired/package_engineering.json)：保留相关观察、外推限制、A/B认识与该论文上下文
- [教育学同型反例：前](records/evidence/baseline/package_education.json) / [后](records/evidence/repaired/package_education.json)：保留延迟词汇记忆改善与未迁移到新语法问题的区别

旧次级打分覆盖BM25顺序，再按段落类型只取一段，是多章节材料丢失的可复现原因。修复并不假定任何一种排名永远正确。

## 实际验证

最终有界离线控制：**251 passed，2 deselected**，其中原238项控制和新增13项。两项排除仍为既有完整模拟链测试，未跑完整规划/正文。

```bash
PYTHONPATH=/tmp/optomind-wo01-deps:. python docs/acceptance/body03-local-lookup-real-20261003/records/evidence/run_offline_controls.py
python -m compileall -q optomind_research/runtime/upgrade3/planning_material_search.py optomind_research/runtime/upgrade3/planning_material_triage.py tests/upgrade3/test_body03_local_lookup_r2*.py
git diff --check
```

依赖路径为云端已有环境，本地可使用自己的项目环境。执行器禁用 socket，真实模型与网络边界均被替换。

- [最终输出](records/evidence/final_controls.txt)
- [主要反例基线失败](records/evidence/baseline_pytest.txt)：6项失败；其中参考文献上下文另以修复代码关闭过滤的受控消融确认
- [独立控制基线](records/evidence/boundary_baseline_pytest.txt)：5失败、2通过
- [上下文过滤消融](records/evidence/context_filter_before_pytest.txt)
- 相同实际输入复用为零新增模型边界调用；仅更改指定论文A卡实质内容后，出现一次新调用，再次运行恢复零新增调用
- 六篇及十二篇大材料提名：新增上下文和片段总和不超过24K，实际内容及来源被保留；空间不足时诚实保留遗漏状态
- 无卡片仍可读正文，只有卡片仍可读已有认识；未满足任务不会因多送材料而被强制完成
- compileall、工作区及暂存区 diff-check 通过

生产修改仅 `planning_material_search.py` 与 `planning_material_triage.py`。根审阅核对 progressive_review_plan、planning_supplement、planning_retrieval_loop、chapter_arrangement、review_unit_writer 文件哈希均不变；Qwen判断器、消息构造器和guardrails的AST不变。全局协调及负责人修订→正式案例附加→编排→写作的顺序不变。

## 剩余限制与本地下一步

- 原问题候选占满十二个槽位时，focus只补这些论文的段落，不新增论文；这是明确的有界取舍
- 原始FTS候选窗口及词法能力仍有局限。BM25、重排与最多三段正文不保证命中所有科学关系
- 大量长段落可用尽预算，A/B互补材料可能被省略；为最终未送入的论文预留的上下文容量可能保守地闲置。十二篇大材料测试实际保住十篇正文/上下文，不宣称全部渠道齐备
- 新增上下文的400字符开头只是导航辅助，不能替代保留条件的正文片段；真实模型如何使用它仍待验证
- 本轮没有原数据库，不声称恢复特定论文段落或提升真实综述质量
- 历史模型将患者外推边界收紧成患者临床验证的倾向仍需观察，本轮未改科学提示词

建议本地固定原问题、判断标准与真实索引，重跑原来的无指定论文/指定稳定身份两种小额场景。先核对实际送入的正文关系、模型条件、A/B认识，再审输出是否形成有用且范围合适的材料。不要求必须命中同一论文，也不以ID出现或token增加为通过标准。

云端零付费、零真实检索/论文下载、零密钥访问、零全稿；仅获取/发布已授权的GitHub代码与归档。交接后停止，不合并，不进入04–07。
