# manuscript_parts_plan v1：使用与验收边界

## 本次完成什么

同一次全局材料理解同时返回 BODY outline 和轻量文章职责卡。一级纲要结合实际工具结果校准职责；现有案例后的 whole-plan review 再返回 `no_change` 或完整 replacement。未增加模型阶段、第二套 planner、parts 路由、案例或段落规划。

生产 schema 位于 `schemas/upgrade3/manuscript_parts_plan.schema.json`。本地 `manuscript_parts.py` 做同形状验证，无新运行依赖。只有 `context` 和三个 PartPlan；每个 part 只有 purpose/focus/boundary/placement/finalize_from。字段少不代表内容空泛，topic-specific focus/boundary 仍需人工与真实生成验收。

新合同以现有 `--planning-revision` 开关启用。关闭开关保留旧模式，不能据旧模式输出来验收新合同。

## 持久化与恢复

- 新规划版本 `optomind.progressive_review_plan.v2`
- 职责合同版本 `optomind.manuscript_parts_plan.v1`
- RUN_STATE、partial、最终 plan 都有顶层职责卡与 v0/v1/v2 标记
- level1/level2 stop 后可用同一新运行目录 resume；旧 RUN_STATE 或无状态的缓存目录明确拒绝
- 先前卡、材料/工具反馈、BODY任务和边界进入相关缓存身份。没有声称重构全部历史缓存或追踪任意外部数据库的原地变化
- 最终 packet 只带轻量只读 boundary、合同版本及 `../DETAILED_REVIEW_PLAN.json`；搬运时保留这一相对目录关系，或显式提供 planning_context
- 源文献身份仍来自同一个完整材料池。未分配到 BODY 的文献不会因此被禁止用于入口背景
- owner revision 未解决、case enrichment 不完整时不把职责合同标记为可交付冻结状态

## 首尾接线

实际正文仍是每个首尾阶段的主要输入。Conclusion → Introduction → Abstract 保留为软件依赖顺序，各阶段加入本部件职责、review_argument、shared_scope、theme inventory、source identity map、显式真实材料以及已生成部件。

`review_delivery` 自动读取新 writer packet / manifest 的 planning_result_path。新合同存在却无法解析关联 plan 时拒绝退回旧模式。

已有 delivery 配置可增加：

```json
{
  "planning_context": {"path": "../new_plan/DETAILED_REVIEW_PLAN.json"},
  "material_records": {"path": "background_excerpts.json"}
}
```

以上仅为增加项，仍需原有 delivery schema、编辑与首尾 fixture/recording 配置。相对路径从配置文件目录解析。若 packet/manifest 已能自动解析 planning context，配置也可只提供 `material_records`，在上下文解析完成后补入并验证；找不到任何真实职责合同时明确报错。`planning_context` 也可显式传紧凑对象，至少包含有效职责卡；真实计划文件需处于 complete/frozen v2，而不能把 partial 作为最终合同。

`material_records` 是已存在的真实 A/B 卡或片段对象列表；总序列化文本超过100,000字符会报错，不静默截断。也可直接传到 API。当前版本不自动检索这些记录；card_path/material_access 是定位信息，不冒充已读取全文，也不会自动打开所有全池文件。与历史正文联合测试时须核对引用身份，同号不同文献会拒绝继续。

## 标题与 placement

- standalone 用 `<!-- manuscript-part:...:start/end -->` 专属范围写入与更新
- 不因标题是 Introduction/Conclusion 替换实质 BODY。深度教学、公式、机制、证据比较继续由正文链完成
- embedded/distributed 在 schema 与规划中合法；本版本无可靠 section identity 来自动应用，返回 unsupported_placement，保存原规划、零生成、阻止后续发布
- anchor 是语义定位，未被当作字符偏移或 string-replace 地址
- 重复运行只更新自己的标记范围。没有标记的历史首尾不会被自动删掉；standalone 与未受标记管理的明确部件标题或已定位的章节职责冲突时，在生成/应用前返回 `placement_conflict` 并阻止后续引用、出版。模糊或未定位的情况仅报告 warning；该检测仅用于出版位置安全，不改变 BODY 分类。做“新卡 + 历史 BODY”测试时须先由人工明确承载决策
- 无合同的旧 fixture 路线仍保留并标为 legacy，不是旧规划迁移

## 离线验证与内容审查

新测试覆盖 schema、三个规划检查点、完整注入式规划流程、partial/resume、缓存失效、旧状态拒绝、BODY/PartPlan隔离、深度 Introduction 和 Outlook 反例、最终 packet 一致性、manifest/assembly/首尾上下文接线、必要背景引用、失败闭锁与标记幂等。

BODY 编排/写作者核心算法没有修改。新合成案例实际经过现有 arrangement→writer payload，原 paragraph briefs、公式、条件、比较、A/B/deep/local/supplement/tool材料与引用身份保持一致。另用仓库内历史稿验证：6章、23小节、178个唯一引用handle都保留；本次小修对其未标记旧首尾触发冲突并停止生成，历史源文件逐字节不改动。无冲突与已有 owned span 的正向样例继续验证重复应用幂等。

这些是确定性信息保留验收，不是模型质量提升证据。职责卡示例和首尾fixture均为人工标注测试材料，不声称它们来自模型。

## 已知基线问题

精简仓库没有 tests 所需完整 outputs 历史资产；一个已有测试将工作目录写死为 `F:/OptoMind-Review-2`。在未修改的基线副本和本分支上分别跑原61项测试，均为49通过、12失败，失败集合一致。没有改旧测试隐藏失败，没有覆盖历史输出。

## 下一步需单独授权的真实验收

1. 提供已有上游 PLAN、完整 A/B pool 及可解析本地材料，设定金额上限；只跑新版 global planning 到 level1，比较 BODY覆盖/主线和卡片具体性，避免完整E2E
2. 第一轮认可后，用新卡与选定的既有23单元 BODY，仅验证首尾生成，逐段亲读承诺、条件、背景材料与综合判断
3. 当前首尾生产接口仍是 replay/fixture；真实首尾调用方式及预算需先明确，不把离线接线完成当成 live调用能力已验收

本轮未调用付费模型、未合并 main/review-v2、未转换或删除历史规划产物。

## 本次实际执行结果

2026-09-30，Python 3.12 / pytest 8.4.2（临时测试依赖目录，不修改项目依赖清单）：

- 新增4个测试文件：55 passed
- 整个根 tests/upgrade3：104 passed / 12 failed
- 未修改基线副本的原61项：49 passed / 12 failed；失败测试名集合与本分支一致
- `git diff --check`：通过
- 修改模块 compileall：通过
- 新规划 CLI `--help`：通过；preflight 注入测试核对新合同提示已进入估算，零模型调用

复验命令（已安装项目所需依赖的环境）：

```bash
python -m pytest -q tests/upgrade3/test_manuscript_parts*.py
python -m pytest -q tests/upgrade3
python -m compileall -q optomind_research/runtime/upgrade3 scripts/upgrade3
```

第二条在精简云端快照中仍会报告上述12个基线失败，不能解读为全套绿灯。新增测试用人工fixture/注入式planner与网络拦截完成；未使用付费模型，未生成新的科学结论。

## 验收后两项小修（2026-09-30）

- 明确的 standalone 承载冲突返回 placement_conflict；冲突清单记录 part、requested_mode、existing_location、reason。原 BODY 不动、不降级 embedded、不写冲突稿；downstream 停在03，不进入04/05
- 仅在文章级 H1/H2 的明确部件名、编号章节标题或能定位的显式 chapter role 上阻断；技术性 Introduction to…、局部三级小节和代码示例不因此阻断。已有 owned marker 从冲突位置中排除
- config.material_records 可与自动解析上下文结合。主合同来源优先级不变，显式列表替换原材料列表后走同一 bounded validator；没有任何上下文仍拒绝
- 材料记录若显式给出与同 handle 的规划身份冲突的 DOI/paper_id，会拒绝；已有 delivery catalog 身份冲突防线保持
- 本次不改 planner/schema/编排/单元写作、不修历史12项失败、不进行真实模型调用。推送后停止开发，由本地Agent在真实材料与预算环境验收

小修验收结果：manuscript-parts 专项 **91 passed**（原55项，新增36项；部分旧断言按新 fail-closed 要求调整）；整个 `tests/upgrade3` **140 passed / 12 failed**。与修改前 tip 的 **104 passed / 12 failed** 对照，失败测试名集合完全一致。新增 placement 26项、material merge 9项及 downstream阻断1项；`git diff --check`、compileall通过。测试/应用流程零网络、零模型调用；Git读取与发布使用正常网络。推送此独立修复提交后停止开发。
