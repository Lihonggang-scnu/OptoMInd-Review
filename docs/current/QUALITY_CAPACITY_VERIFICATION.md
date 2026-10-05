# 质量预算改造的工程验证

基线：`43c1c940f85d92fe414590db1820d73d25a96084`。本轮提高现有链路预算并修复材料字段交接，没有新增规划器或重跑科研正文。

## 实际请求已经检查

使用真实 `QwenDirectClient` 构造HTTP请求，在网络边界截获参数并返回标注的离线响应：

- 局部强修订：32,768思考＋32,768回答 → `max_completion_tokens=65,536`
- 整章强修订：32,768思考＋65,536回答 → `max_completion_tokens=98,304`
- 编排：16,384＋32,768 → 49,152
- 写作/补写：8,192＋32,768 → 40,960
- 请求没有混入 `max_tokens` 或冲突的 `reasoning_effort`
- 每次实际请求的参数回执与账本重开后的记录一致
- 显式开启/关闭思考、调用者自行设置预算、从关闭的client重新开启思考，都有针对性检查

十种角色的配置及捕获结果见 [QUALITY_CAPACITY_EVIDENCE.json](QUALITY_CAPACITY_EVIDENCE.json)。本地可直接运行 `tests/upgrade3/test_quality_capacity_profiles.py` 与 `test_qwen_effective_token_transport.py` 复核。

## 原来丢掉的信息已经送到

复用细纲实验归档 `413177100053d9edd588c5e44955e21531cc6710` 的四组真实任务，重建编排和writer消息：

| 内容 | 修前进入编排/writer | 修后进入编排/writer |
|---|---:|---:|
| finding_conditions | 0/19 | 19/19 |
| argument_relations | 0/8 | 8/8 |
| 原point/development | 27/27 | 27/27 |

writer重放使用归档已经接受的 `ARRANGEMENT_FOR_WRITER`，结合新代码重建的负责人上下文。材料选择保持原记录，不重新运行来源路由。多领域受控用例同时检查字符串、列表、结构化条件、合并/拆分任务及补写。

## 缓存与恢复没有把新配置换回旧值

- 每个规划阶段的实际调用、上下文预估、分批估算和缓存签名使用同一有效设置
- 改变思考或回答预算会改变适用的自动缓存合同
- 定向精读的新运行参数进入任务身份和独立目录；旧材料保留，显式历史材料仍可使用
- 修复了在“较早任务→局部回答→恢复较早任务→再次恢复局部回答”时，把回答重复塞进自己的历史、造成负责人缓存失配的问题
- 引用别名的序列化顺序固定，正常执行与缓存恢复的消息一致；引用映射含义不变

## 测试命令与结果

在独立Python环境中执行：

```bash
python -m pytest -q tests/upgrade3 --tb=short
python -m compileall -q optomind_research/runtime/upgrade3 scripts/upgrade3
git diff --check
```

全套结果：**1,262通过，8个subtests通过；12项依赖缺失历史文件或固定Windows路径的既有失败**。这12项在基线43c1c940的独立工作树中逐项复现，失败集合一致。结构化记录见 [QUALITY_CAPACITY_TEST_RESULTS.json](QUALITY_CAPACITY_TEST_RESULTS.json)。

新增测试覆盖有效预算、真实HTTP参数、CLI/反馈入口、精读store、缓存恢复和条件/论证关系的实际消息消费。编译及diff检查通过。

本轮的具体交付是：模型得到更充足且真实生效的配置，已有知识任务与条件完整送达。下一步按开工交接直接推进细纲与写作改进，无需先做一轮大小额度优劣实验。
