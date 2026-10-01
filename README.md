# OptoMind-Review — 研究与写作链路

本分支是持续升级中的精简源码版，当前保留上游冻结点 review-v2-content-handoff-20260929，并加入2026-09-30离线交付层；云端采用精简快照，不携带本地完整开发历史。
比赛提交版和技术报告中引用的代码保留在 [main](https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/main)；本分支不会替换 main 或原有报告链接。

## 链路与代码入口

| 环节 | 职责 | 主要代码 |
| --- | --- | --- |
| 研究问题分解与检索规划 | 将问题拆成 Facet、关键词查询、语义检索式与范围 | `optomind_research/runtime/upgrade3/query_plan.py` |
| 学术检索与候选语料 | 多通道检索、论文归并、候选材料集合 | `optomind_research/runtime/upgrade3/candidate_corpus.py` |
| 学术关系骨架 | 根据现有材料及引用关系组织候选研究 | `optomind_research/runtime/upgrade3/scholarly_skeleton.py` |
| 本地材料工具层 | 多来源获取材料，解析正文；PDF 可使用 GROBID | `optomind_research/runtime/upgrade3/local_materials.py`、`tools/academic_backends/` |
| 阅读与材料卡片 | 通用理解 A、面向当前综述的规划材料 B、问题导向解析 | `scripts/upgrade3/paper_reading_card.py`、`scripts/upgrade3/paper_reading_batch.py`、`scripts/upgrade3/module4.py` |
| 材料补充与定向精读 | 本地优先补取材料，按具体需求补充与阅读 | `scripts/upgrade3/planning_supplement.py`、`scripts/upgrade3/directed_reading.py` |
| 逐级规划 | 范围、章节分工、细纲、案例与全局协调 | `optomind_research/runtime/upgrade3/progressive_review_plan.py` |
| 编排、写作与汇编 | 段落/表格任务、单元正文、全文与参考文献 | `scripts/upgrade3/chapter_arrangement.py`、`review_unit_writer.py`、`full_review_draft.py` |

`run_review_harness.py` 保留原有综合入口；升级模块还提供独立 CLI，不能把源码齐全理解成所有新模块已经默认串入综合入口。

新增规划修订路径通过 --planning-revision 显式启用，默认关闭以兼容旧调用。它已经冻结为当前阶段唯一的升级开发基线；旧模式仅作兼容，不是并行候选方案。冻结不代表论文内容零错误，已知边界见 [冻结基线说明](docs/REVIEW_V2_FROZEN_BASELINE.md)。

## 本地运行准备

使用 Python 3.11+，在仓库根目录安装依赖：

```powershell
python -m pip install -r requirements-research.txt
python run_review_harness.py --help
python scripts/upgrade3/progressive_review_plan.py --help
python scripts/upgrade3/review_unit_writer.py --help
```

凭据需自行在本机配置，具体环境变量或密钥文件位置见 `config/qwen_config.py`、`config/secret_pool.py` 和各后端配置。`api_keys/` 不随仓库发布。Qwen 调用沿用直连客户端；付费运行前配置明确预算。`--help` 只查看参数，不发起模型调用。

PDF 结构化解析可使用 `deploy/grobid/docker-compose.yml`，默认访问 `http://127.0.0.1:8070`。各模块的输入与调用说明见 `docs/`；示例问题见 `examples/research_question.json`。示例只是任务输入，不是已验证的综述成果。

## 发布范围

保留链路源码、提示词、运行时 schema、必要配置、接口说明及一个问题示例。没有包含真实密钥、论文全文、数据库、模型响应、历史工单、测试运行产物或静态回放站点。本分支使用精简快照，不携带本地开发历史。

## 本次更新（2026-09-29）

修复 A/B、精读、补充与本地片段从章节负责人到写作者的传递；案例建议须交给负责人结合真实材料采纳；改进局部缺口、跨章分工的提示要求，以及部分完成稿的汇编和引用编号。

本次进一步保留章节负责人原任务及其条件、指标和来源关系，使编排负责顺序与拆合，写作者逐条消费原任务后再综合。该实现已冻结为下一阶段开发基线；测试结论、限制和后续方向见 [冻结基线说明](docs/REVIEW_V2_FROZEN_BASELINE.md)。

## 2026-09-30完整综述交付层

正式入口通过 `--delivery-start history|plan --delivery-config 配置路径` 连接装配→全文编辑→结语/引言/摘要→图表引用→Markdown/TeX/PDF。当前交付分支使用录制响应或标注fixture；不声称已验证真实模型投稿质量。

- [当前实现与验收范围](docs/REVIEW_V2_DELIVERY_STATUS_20260930.md)
- [外部顾问阅读包：稿件、重要测试、30篇范例、AI意见](advisor/20260930/README.md)
- [统一入口](optomind_research/runtime/upgrade3/review_delivery.py) / [全文编辑](optomind_research/runtime/upgrade3/article_text_editor.py) / [首尾提炼](optomind_research/runtime/upgrade3/manuscript_front_back.py) / [图表引用](optomind_research/runtime/upgrade3/delivery_citations.py)

顾问阅读包与运行输入分离，未来可独立删除。`main`保持旧技术报告链接不变。

### lihonggang：正文后独立首尾（显式启用）

此分支新增独立后置构思与首尾生成，不改变 `lihonggang` 的正文 planner、编排或单元写作。使用说明与离线/真实测试边界见 [POST_BODY_MANUSCRIPT_PARTS.md](docs/POST_BODY_MANUSCRIPT_PARTS.md)，设计边界见 [IMPLEMENTATION PLAN](docs/POST_BODY_PARTS_IMPLEMENTATION_PLAN.md)。独立入口为 `scripts/upgrade3/manuscript_parts.py`；既有 delivery 仅在 `front_back.mode=post_body` 时选择新模块。离线通过不代表模型内容质量已通过。
