# OptoMind-Review — 研究与写作链路

本分支是持续升级中的精简源码版，来自本地版本 `c52079f`（2026-09-28）。
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

新增规划修订路径通过 `--planning-revision` 显式启用，默认关闭。它增加材料访问、章节负责人更新及有界反馈回路。目前真实样例仍出现案例归属和综合判断偏差，尚未冻结为稳定默认路径。

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
