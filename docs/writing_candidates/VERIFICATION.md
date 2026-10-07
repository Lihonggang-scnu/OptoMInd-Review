# 写作候选路线：独立离线验收

日期：2026-10-07。基线：`1615f41e629c92f6c04b440846f5505eec386758`。

## 结论与范围

本报告核验新增写作路径是否真的按声明执行、是否忠实传递现有任务与材料、是否在失败时保留正文和真实状态。它不是三种模型写作质量的排名。测试没有调用付费服务，没有执行完整生产 BODY，也没有把人工写的科学答案加入生产提示词。

独立集成测试文件：`tests/upgrade3/test_writer_candidates_integration.py`。除本地提示词副本用于验证缓存失效外，仅替换模型／凭据／网络边界；正式编排 CLI、材料构建、投影、候选引擎、解析器、缓存、磁盘产物、Qwen 请求封装、SSE 解析及 SQLite 账本均实际运行。

## 使用的历史材料

### 按需加强 Ch2 档案

入口为 `docs/verification/on-demand-efficiency-20261007/` 下的 `UPDATED_PLAN.json`、`integration_handoff/OWNER_MESSAGES.json` 和 `OWNER_RESPONSE.json`；其消费依据见 `docs/verification/on-demand-promotion-20261007/README.md` 与 `DOWNSTREAM_GUIDANCE.json`。

- 采用已保存的真实 OWNER 解析结果：两个单元、七条任务、十个案例、十八条研究限制
- 采用公开导出的十八条选中材料记录，经既有身份机制归并为十六个规范来源
- 由真实 `project_plan_for_arrangement` 建包，再经正式 `chapter_arrangement.py --reexport-from` 生成可消费编排与视图
- 编排返回和新 writer 返回是明示的受控离线响应，不是新模型结果
- 逐字段比较负责人案例、限制、综合衔接、完整任务与材料；随后检查实际保存的候选消息

这个档案只有已选任务簇及其公开材料，不是原始完整 Ch2，也不是原私有 367 条材料池。路线 A 的“全章一次调用”验收指整个可用的两单元测试包，不能据此宣称已重放原完整章节或原始收费请求。

### 质量扩容 Ch1 档案

另检查 `docs/acceptance/quality-capacity-20261006/evidence/` 中的 `03_revised_outline.json`、`04_approved_arrangement.json`、`11_materials_A_B.json` 与 `13_writer_U1_request_view.json`。

测试用公开细纲、保存的 chapter frame 和来源目录，经真实 arranger 视图构建恢复规范任务身份；被批准的编排原样消费。逐字段检查公开 A/B 与完整工具记录，特别检查被拒绝的旧来源句柄不重新取得引用身份。

该包的 README 明确指出：请求视图省略本地定位、密钥路径、完整 card material、论文全文和原文段落。因此这个测试是公开档案的消费者核验，不是字节级重放原请求。档案中的 A/B 本身也可能含历史误读；保真传递不等于验证其科学真实性。

### 跨领域结构控制

另一套明示为虚构的太阳能电池材料用于测试：恢复与寿命边界、结构化条件、空列表／null／false／零值、DOI 确认别名、互补 A/B、完整表格、长材料末尾标记，以及含冒号、斜杠、反斜杠和 Windows 保留字的任务身份。它只测试机制，不能作为领域科学证据。

## 独立集成覆盖

1. **路线 A 的真实整包调用**：正式 CLI 只产生一次 `writer_chapter` 调用；所有七条历史任务及相关完整材料出现在实际消息中；没有隐藏单元循环或额外 editor
2. **路线 B 的实际编辑**：两个单元 writer 后执行一个 editor；编辑器收到真实初稿、完整任务和材料；最终正文取自编辑器，确实不同于初稿拼接；表格内容原样保存
3. **路线 C 的真实容量分批**：确定性的测试计量器及 7,500-token 输入上限迫使单元内部按完整任务切分；每个任务恰好分配一次；所有被选材料逐字段相同、末尾仍在；多个编辑窗口实际执行，并带可容纳的只读邻接正文
4. **原子超限**：完整任务及所需材料仍放不下时没有任何模型调用；保存任务／材料清单及完整输入；未显式授权时不切换路线
5. **表格漏交与失败恢复**：编辑器声称完成任务但没有实际表格时，最终状态仍 pending，保留可用原稿与表格；普通恢复不自动再调用；显式重试只重试失败 editor，已成功 writer 不再调用
6. **缓存真实失效**：相同输入与设置复用；writer 思考额度、材料、任务和实际提示词文件分别改变时产生新请求；旧 raw 文件不变
7. **解析中断恢复**：模拟 raw 已保存、RESULT 尚未落盘，恢复会从原 raw 重新解析，不增加模型调用
8. **真实流式协议与费用账本**：使用正式 CLI `--run` 路径和真实 `QwenDirectClient`，只替换 HTTP 边界；核对 writer/editor 实际 wire 的思考额度、合并输出额度、stream、请求超时及流总超时；三笔响应通过实际 SSE 解析后结算到同一 SQLite 账本
9. **预算及传输失败**：极低共享预算在 HTTP 前拒绝，实际发送数与账本记录均为零；503 每个独立 writer 只有一次请求，无自动重试，费用保留为不确定占额；恢复不会重复发送
10. **CURRENT_PLAN 接线**：移除临时编排目录的相邻视图后，经相对 `CURRENT_PLAN.json` 指针找到真实 packet root，实际材料构建与任务投影仍一致
11. **下游 BODY 装配**：一个正文块可跨两个原单元；两个章节严格按显式 manifest 顺序装配，不制造 `UNIT_RESULT.json`，不重新拆正文；后续真实容量失败保留旧有效章稿，但阻止将当前未完成运行当成最终 BODY；显式 draft 导出有可见标记，旧完整 BODY 不被覆盖
12. **身份与来源边界**：别名在一个规范来源下保留互补材料；Windows 不安全任务 ID 保留在内容中，但不会成为不安全文件路径；冲突记录的旧句柄只保留为溯源信息

本次实际阶段结果：A 为 1 个 writer；B 为 2 个 writer + 1 个章级 editor；C 为 4 个 writer cluster + 4 个 editor window，每批各承接一个完整任务。C 各 writer 输入估计为 5,096／4,995／5,003／5,003，editor 为 5,222／5,215／5,227／5,137；实际值与消息哈希保存在证据 JSON。

容量控制中的 7,500 是确定性测试计量器的输入上限，不是推荐模型容量或生产 tokenizer 结果。SSE 控制响应的 usage 也是测试数据，不是实际收费。

## 独立审查发现

集成过程发现并交回对应实现者修复，而非改写测试绕过：

- CLI 导入的 `validate_config` 最初未导出，真实正式入口无法执行
- 引擎最初读取不存在的 `reserved_input_tokens` 估计字段，导致所有真实路线在调用前停止；现以运行时实际估计字段计算并单独记录
- 对缺 ID 的旧任务，子集投影曾重排生成身份；现由显式 `task_keys` 保留原目录身份，原始任务内容不被改写
- 预算拒绝最初只留在 stage 内部结果，公开报告丢失 `global_budget_exceeded`；现将具体 blocker 带入 stage 与最终问题列表，区分 client invocation 与可确认的 paid dispatch
- 质量扩容真实工具记录中的 `original_source_handle` 曾被当成活跃依赖，导致已拒绝的 P0594 被重新建为来源；已修复为只对接受的来源字段建立依赖，历史身份信息保持只读溯源
- 历史质量扩容章框架中仍有旧 scope 与新版 chapter argument 不一致；科学字段不能靠计数验收，已在 writer/editor 提示词中加入通用输入优先级，保留研究范围边界并说明详细可归属材料决定事实支持，没有注入本题特定答案

以上运行状态与最后回归结果以仓库最终测试为准。`model_calls` 的兼容字段表示 client invocation attempts，可能包含离线响应和调用前拒绝；判断收费必须看 `execution_mode`、`paid_dispatch_count`、transport 证据和共享账本。

## 可复现命令与当前结果

在项目已安装依赖的 Python 环境执行：

```bash
python -m pytest -q tests/upgrade3/test_writer_candidates_contracts.py tests/upgrade3/test_writer_candidates_engine.py tests/upgrade3/test_writer_candidates_cli.py tests/upgrade3/test_writer_candidates_integration.py
```

当前核验结果：**13 个独立集成测试通过；全部候选专项测试 101 passed**。小型可审阅证据见 [`IMPLEMENTATION_EVIDENCE.json`](IMPLEMENTATION_EVIDENCE.json)，其中保存输入／实现文件哈希、真实消息中选取的完整任务／案例／限制、实际 editor 初稿块、分批明细、wire 参数与账本控制结果。最终全量回归由集成负责人另外记录。

既有消费者边界回归已执行：

```bash
python -m pytest -q \
  tests/upgrade3/test_promotion_downstream_owner_guidance.py \
  tests/upgrade3/test_outline_promotion_pipeline.py \
  tests/upgrade3/test_on_demand_material_adoption.py \
  tests/upgrade3/test_review_unit_writer_portable_paths.py \
  tests/upgrade3/test_quality_capacity_profiles.py \
  tests/upgrade3/test_qwen_effective_token_transport.py
```

结果：**127 passed, 10 subtests passed**。这是有针对性的既有边界回归，不等同于整个 `tests/upgrade3` 全绿。

## 尚未证明的事情

- 未验证真实付费模型在三种路线下的科学写作质量、章节连贯性、忠实性或费用收益
- 未重建公开包中不存在的完整私有材料池／原始本地请求
- 未在 Windows 原生执行；只在 Linux 实际落盘，并逐个检查 Windows 文件组件规则
- Markdown 表格存在、引用诊断和任务 ID 覆盖均是结构检查，不能证明每个案例、比较或限定条件已被正文正确展开
- 显式 BODY 装配只验证章节选择、顺序、来源和状态；没有把候选章节伪装为旧 unit delivery，也没有宣称已完成旧交付链的参考文献／图表处理

下一轮若获授权，应使用本地完整已批准输入，固定材料与模型设置，先核对实际执行和费用，再分别审读初稿与编辑稿；实现验收和科学质量验收继续分开。

## 最终冻结回归

新增候选专项 **101 passed**。整个 `tests/upgrade3` 为 **1588 passed, 17 failed, 10 subtests passed**；未修改基线为1487 passed、同17项失败、10 subtests passed。失败集合逐项相同，见TEST_RESULTS.json。编译、CLI帮助与差异空白检查通过。云端付费调用为0。
