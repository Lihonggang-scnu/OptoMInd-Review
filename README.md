# OptoMind 新版完整研究与写作链路

本分支 `lihonggang-dev` 是新版唯一开发入口。原比赛/技术报告代码保留在 `main`，本次整理不修改它。

**先读 [当前链路与状态](docs/current/PIPELINE.md)，再读 [给 AI 助手的阅读顺序](docs/current/AI_READING_GUIDE.md)。** 不必从几十个历史工单猜当前实现。

开发宗旨：[严格拒绝过度防御，以产品质量和实际帮助为先](docs/current/PRODUCT_PRINCIPLES.md)。最新预算与材料交接更新、下一轮本地开工入口：[QUALITY_CAPACITY_HANDOFF.md](docs/current/QUALITY_CAPACITY_HANDOFF.md)。

## 三个分支的职责

| 分支 | 用途 |
|---|---|
| `main` | 最初提交版，原历史链接与代码保持不变 |
| `lihonggang-dev` | 当前完整源码、必要测试与唯一后续开发入口 |
| `archive/history` | 全部旧分支的固定提交、历史资料及恢复索引，不是当前生产实现 |

[历史归档与旧分支映射](https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/archive/history)。归档保留完整 Git 历史，不把失败方案的算法混进新版。清理旧分支名称不改变固定 SHA 的历史文件树。

## 新版包含什么

研究问题与检索计划 → 候选文献 → 学术关系骨架 → 本地材料获取/解析 → 单篇理解及 A/B 卡片 → 逐级 BODY 规划与补充阅读 → 全局协调和负责人修订 → 案例正式附加 → 编排 → 单元写作 → 全文装配。

另保留以下**显式启用**的下游模块：
- 全文写作候选：全文一次写、连续作者、长文工作台、读者驱动修订；完整 BODY 为交付单位，见 [说明与100元本地测试入口](docs/fullbody_writer/README.md)
- 细纲加强：第一层自主选择同章任务簇，按需 Max 完整回读并局部加强，合回完整章节后交给既有编排/写作；入口与恢复见 [按需正式接入](docs/verification/on-demand-promotion-20261007/README.md)
- 独立首尾模块：正文后构思、结语、引言、摘要/题名，保留原 BODY；目前不代表首尾写作质量已通过
- A/B/C 局部修订实验：固定正文、按材料提出和核验局部补丁；仍是实验候选，不能自动替代正式稿或宣称质量胜出

当前源码来自 `02c018bdd440cc061ddedbb5aef07b115e7de081`，包含已验收的 BODY40 身份/引用/alias 修复及下游修订实验。独立首尾代码从 `dc661cf47bdb8ec70a348c02bc8f15a0b40253f4` 有界接回，保留当前装配未决阻断；没有合入早期并行 `manuscript_parts_plan` 对 BODY 的改造。

在上述整合基线之上，当前已加入质量优先预算、实际请求参数记录、条件与论证关系交接修复；具体源码变化和验证见 [QUALITY_CAPACITY_VERIFICATION.md](docs/current/QUALITY_CAPACITY_VERIFICATION.md)。`CORE_SOURCE_INVARIANTS.json` 是分支整理当时的历史校验，不是后续源码永远不变的约束。

“源码完整”不等于所有模块已由一个命令默认串行启用，也不等于完成了新一轮端到端科学质量验收。具体 API、CLI、显式开关及验证边界见 [PIPELINE.md](docs/current/PIPELINE.md)。

## 本地准备

Python 3.11+；依赖见 `requirements-research.txt` / `pyproject.toml`。凭据仅在本地配置，不提交密钥、数据库或权利不明论文全文。任何真实模型运行须使用明确预算。

```bash
python -m pip install -r requirements-research.txt
python scripts/upgrade3/progressive_review_plan.py --help
python scripts/upgrade3/full_review_draft.py --help
python scripts/upgrade3/manuscript_parts.py --help
python scripts/upgrade3/post_body_revision.py --help
```

GROBID 相关部署保留在 `deploy/grobid/`；`run_review_harness.py` 保留既有综合/兼容入口，新模块是否接入须沿实际调用核查。

## 历史材料为什么仍有一部分在目录中

`docs/acceptance/`、`docs/workorders/` 含验收资料和离线测试依赖，暂保留原路径避免破坏测试。它们不是当前开发规范；按明确问题才读对应记录。原 `advisor/` 阅读包已从当前工作树移出，完整内容可从归档索引的原提交恢复。

整理范围、备份校验与来源证明见 [BRANCH_CONSOLIDATION.md](docs/current/BRANCH_CONSOLIDATION.md)。
