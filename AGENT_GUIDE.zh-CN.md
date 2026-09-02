# 给 AI Agent 的 OptoMind-Review 项目说明书

> **这是一份专门写给 AI Agent 的说明书，不是给普通终端用户阅读的 README。**
> 你的任务是理解、运行、诊断和评价 OptoMind-Review；不要把它当成一个需要
> 重新设计的空项目，也不要把内部运行日志、密钥内容或未经授权的远程操作带入
> 处理过程。

本文先介绍项目做了什么、怎样做、目前做到什么程度以及它的贡献和边界；随后给出
一套 Agent 可以直接执行的启动、监控、恢复和验收协议。所有路径均相对于仓库根目录，
不要把某台机器上的绝对路径写入命令、配置、报告或提交内容。

## 0. 当前统一入口

新机器优先使用仓库根目录的统一入口，不要先手工拼接完整 Harness 命令。Windows 用户可
双击 `START_OPTOMIND.cmd`；Agent 应优先在终端执行 `python quickstart.py doctor`，随后
执行 `python quickstart.py ui`，以便保留可观察的检查和运行日志。macOS 或 Linux 使用
`python3 quickstart.py doctor` 和 `python3 quickstart.py ui`。

统一前端中的静态回放不需要密钥；“检查并准备真实运行”会检查项目资产、仓库根目录下的
`api_keys/`、Python 科研依赖、Qwen 模型服务和学术文献服务。快速真实验证使用 3 CNY
全局预算并跳过图像生成、中文翻译和 PDF 编译；完整自主综述使用 15 CNY 全局预算，
沿用三次正式 E2E 的出版主线。两种新运行都写入 `local_runs/`，不得覆盖 `replay/` 或
`artifacts/e2e/` 中的三次正式记录。Agent 不应承诺固定墙钟时间，也不得在用户未明确
要求真实运行时消耗模型额度。

## 1. 一分钟理解项目

OptoMind-Review 是一个面向科学文献综述的研究型 Harness。它的核心不是“让一个模型
直接写一篇文章”，而是把一条自然语言研究请求逐步转换为：

```text
研究问题
  → 主题与范围身份
  → 论文、开放全文和正文片段
  → 中央材料库与主题知识库
  → 按章节组织的证据包
  → 论点卡与生产交接
  → 各章节草稿
  → 章节解释与代表性应用加强
  → 全文结构、标题、摘要、引言、结论
  → 视觉资产与引用元数据
  → 英文/中文 LaTeX 与 PDF
```

项目目前主要在光学和电磁学题目上完成了完整验证，但领域知识和任务说明主要由
`prompts/` 与 `domain_config.yaml` 驱动，因此它不是把光学术语硬编码进一个单一脚本。
换领域时，首先应检查提示词、领域配置、检索后端和评价门，而不是直接重写主链。

它的正确定位是“证据约束的研究到出版流水线”，不是文献数据库、不是单纯搜索引擎、
不是只会生成段落的聊天机器人，也不是已经获得同行评审认证的自动科学家。

## 2. 主链做了什么

主入口是 `run_review_harness.py`。下面的 13 个阶段是当前默认出版主线的实际职责：

| 阶段 | 任务 | 关键实现 | 主要结果 |
| --- | --- | --- | --- |
| 1 | 启动与配置 | `run_review_harness.py`、`config/`、`domain_config.yaml`、`prompts/` | 运行目录、配置快照、预算预检 |
| 2 | 理解用户问题 | `query_planner.py`、`runtime/topic_identity.py` | 主题、范围、章节任务和 `TOPIC_IDENTITY.json` |
| 3 | 找资料 | `s2_discovery.py`、`s2_fulltext_acquisition.py`、`s2_text_chunk_retriever.py` | 论文元数据、合法开放全文、可追踪片段 |
| 4 | 中央材料库 | `runtime/central_material_cache.py`、`runtime/topic_scoped_kb_stage.py` | 可复用的材料单元和主题隔离知识库 |
| 5 | 按章节整理证据 | `section_coverage_orchestrator.py`、`evidence_packet_parser.py`、`evidence_portfolio_selector.py` | 章节覆盖决策、证据组合、来源账本 |
| 6 | 形成论点 | `phase3_argument_orchestrator.py`、`r3_production_handoff.py`、`r4_phase3_artifacts.py` | 论点卡、主张—片段—论文的绑定 |
| 7 | 写章节初稿 | `compact_section_authoring.py`、`section_authoring_assets/` | 证据约束的章节草稿和引用映射 |
| 8 | 章节资产加强 | `chapter_asset_enhancer.py` | 通俗解释、代表性应用、解释性引用 |
| 9 | 全文交接与司令员 | `full_manuscript_handoff.py`、`global_manuscript_commander.py` | 章节合并、重复治理、全文结构 |
| 10 | 文章完成 | `staged_article_completion.py`、`article_completion_runner.py` | 标题、结论、引言、摘要和安全的编辑意见 |
| 11 | 视觉规划与挂载 | `visual_editor_runner.py`、`article_visual_asset_planner.py`、`visual_evidence_factory.py`、`visual_procurement_pipeline.py` | 图像契约、来源图/数据图/概念图和排版挂载 |
| 12 | 引用信息补全 | `publication_metadata_resolver.py`、`openalex_metadata_provider.py`、Crossref/Semantic Scholar 适配器 | 作者、年份、期刊、DOI、S2 等元数据 |
| 13 | 生成 PDF | `latex_publication_renderer.py`、`bilingual_publication.py` | 英文/中文 LaTeX、PDF、引用和排版审计 |

当前快速路径有两个很重要的默认策略：

1. 可选的研究计划分支不是当前主线必需部分。需要运行普通综述时显式使用
   `--no-research-plan`，不要因为没有研究计划产物就判定综述主线失败。
2. 昂贵的全局 Phase 3 DAG 默认关闭。除非评测者明确要求更长的全局关系推理，否则
   不要擅自添加 `--phase3-llm-dag`；它会显著增加时间和成本，数十个小时与内存爆炸。

模型只负责填充受约束的内容字段；本地 Python 代码负责 schema、ID、指纹、状态、来源
和门控。Agent 不应把模型输出中的 `status`、`blocking`、`finding_id` 或成本数字当成
最终事实；这些字段以本地运行时重算和写出的审计文件为准。

## 3. 它的贡献和独特优势

### 3.1 相比单次提示词综述

单次提示词可以很快生成看似完整的文章，但难以回答“这一句到底由哪篇论文的哪段内容
支持”。OptoMind-Review 把证据先变成章节可用的材料包，再形成论点卡，最后允许作者
使用这些有权限边界的论点。这样可以把检索、证据选择、论证和写作拆开审查。

### 3.2 相比搜索加摘要系统

它不把每次搜索结果都当成同等可靠的上下文，而是提供：

- DOI、S2 ID、题名/年份等论文身份归一化；
- 主题身份和主题门，阻止历史上不相关的知识库污染当前题目；
- 中央材料缓存和主题范围知识库，减少重复联网和重复解析；
- 章节覆盖和来源账本，要求章节观点有相应的证据组合；
- 论文元数据、正文片段、视觉来源和最终引用之间的可审计记录。

### 3.3 相比单体式长循环 Agent

主链被切成可观察、可恢复的阶段。每个阶段有自己的产物、事件、成本和校验结果；
中断后优先复用已验证的资产，而不是从头支付一遍。重复相同验证失败三次会触发
断路器，防止 Agent 无限支付同一种错误。

### 3.4 相比只生成图片的视觉模块

视觉模块先问“这张图对哪一章、哪一个论点、哪一个阅读动作有帮助”，再进行来源图审计、
数据图绘制或概念图生成。图像不是科学证据，概念图会被标记为解释性或 AI 辅助生成；
图像生成失败或预算不足时，正文不会被删掉，也不会把未完成需求冒充成已挂载图片。

### 3.5 相比只输出英文 Markdown 的报告器

它继续处理全文交接、标题/摘要/引言/结论、引用元数据、视觉挂载和 XeLaTeX/PDF
编译。英文 PDF 是主要交付物，中文 PDF 是附属翻译交付物；翻译层不能改变英文证据、
引用标识、数字、公式或图像。

## 4. 三次完整 E2E 做到了什么

三次历史运行都从模拟用户问题开始，经过检索、证据组织、章节写作、全文完成、视觉
处理和出版编译。以下是最终英文稿和真实运行投入的近似统计：

| 主题 | 英文正文 | 章节 | 引用文献 | 英文 PDF | 活跃运行时间 | 模型调用 | Token | 成本 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 光学衍射神经网络 | 约 13,700 词 | 7 | 121 | 37 页 | 约 2 小时 7 分 | 240 | 约 8.242M | ¥5.39 |
| 超表面全息：从逆向设计、制造到动态显示与成像 | 约 12,900 词 | 7 | 108 | 36 页 | 约 1 小时 34 分 | 444 | 约 10.000M | ¥6.43 |
| 规模化光子计算：从可编程集成光子芯片到 AI 加速与光互连 | 约 16,200 词 | 8 | 124 | 44 页 | 约 1 小时 59 分 | 259 | 约 9.521M | ¥9.66 |
| **三次合计** | **约 42,800 词** | **22** | **353** | **117 页** | **约 5 小时 40 分** | **943** | **约 27.764M** | **¥21.47** |

三次合计约 24.790M 输入 Token、2.974M 输出 Token。模型调用数包括章节审查、返修和
必要重试，不包括普通工具调用；运行时间是活跃墙钟时间；词数来自最终英文正文，
页数来自英文 PDF，总引用数来自最终 bibliography。

### 4.1 三次运行中的风格治理

章节风格治理确实运行过，而且是“审稿人—返修作者”分离的 Qwen 调用，不是只有固定
字符串替换：

- 章节级报告记录了 reviewer 和 reviser 调用；历史三次合计为 44 次，即 22 次审稿、
  22 次返修。
- 全文风格收敛报告记录了实际的段首改写。第一、二、三次运行中，模板段首占比都
  有明显下降；第三次运行中 `While` 从 24 段降到 0，`Building on` 从 20 段降到 0。
- 章节治理报告可能同时出现 `enabled=true`、`changed=true` 和
  `promotion_eligible=false`。这表示局部治理实际改过文本，但全局指标没有达到
  系统设定的“可提升/可晋级”阈值；它不等于治理模块没有运行。
- Agent 评估风格时，应同时阅读 `STYLE_CONVERGENCE_REPORT.json`、
  `CHAPTER_STYLE_GOVERNANCE_REPORT.json` 和最终英文稿，不能只看一个全局 promotion
  字段。

### 4.2 三次运行中的视觉结果

- 第一次运行最终没有可渲染图片，视觉机会有 7 个未填充；视觉成本报告显示没有
  视觉模型调用。这是预算和可渲染资产门控后的结果，不代表视觉模块未接入。
- 第二次运行生成并挂载了 3 张概念图，另有 4 个未填充机会。
- 第三次运行进行了 7 次视觉审查、2 次图示规格调用和 6 次图像生成，最终挂载
  2 张概念图，另有 5 个未填充机会。

视觉预算是全局预算下的硬子预算。图像生成会先预留成本；剩余预算不足时，概念图
可能在生成前被跳过。质量优先的新运行可考虑给视觉模块约 5 CNY 或更高的独立额度，
但必须保证全局预算足够，且不能设置 `--no-real-image-generation`。需要查看：

```text
visual_editor/final/VISUAL_COST_REPORT.json
visual_editor/final/FINAL_VISUAL_PACKAGE.json
visual_editor/final/FINAL_VISUAL_AUDIT_REPORT.json
```

### 4.3 已记录的历史缺陷并不等于英文交付失败

第一、二次运行各记录过一次 `authoring_revision / ExceedMaxIters`：一次是
`max_iters=18`，一次是 `max_iters=24`。两次最终都保留了英文交付物。作者阶段达到
迭代上限时，正确做法不是立即删除章节或重跑整条链，而是检查最后一个有效候选、
章节状态和下游增强结果。当前作者链会持久化 `last_valid_candidate`；只要候选满足
最低结构和证据条件，后续章节加强器和全文交接仍可继续处理它。

第三次运行没有记录运行时错误。三次运行的总体 `delivery_status` 仍可能显示
`degraded`，原因主要是中文翻译审计警告；三次英文 PDF 都可用。因此评估英文主交付
时，应以英文 PDF、英文出版审计和英文引用完整性为主，不要把中文附属层的警告直接
升级成英文主线失败。

## 5. Agent 的安全操作契约

在开始任何运行前，Agent 应先做以下事情：

1. 读取本文件、`README.md`、`README.zh-CN.md`、`docs/MAINLINE_FILE_MAP.md`、
   `config/model_policy.yaml` 和 `domain_config.yaml`。
2. 确认当前工作目录就是仓库根目录，所有输入输出使用仓库相对路径。
3. 只读取 key 文件的存在性、文件名和字节数；不要把 key 内容打印到终端、报告、
   聊天记录、异常信息或模型提示词中。
4. 如果更大的工作区中存在其他支线、历史运行树或本地数据库，先确认它们是否属于
   当前任务；不相关的支线和缓存不得被扫描、合并或覆盖。
5. 不要自动提交、推送、改变仓库权限、安装浏览器登录态或读取受保护全文。除非
   人类明确授权，这份说明书只授权本地检查、运行和诊断。
6. 不要把 `--mock-query-planner` 当作真实 E2E。它只适用于离线结构测试；真实综述
   需要可用的 Qwen 凭据和联网检索条件。
7. 不要用一个全局“成功/失败”字符串代替产物验收。运行结束后必须检查 PDF、引用、
   状态、成本和审计文件。

## 6. 如何在新机器上启动

### 6.1 Python 与依赖

Python 3.11 或更新版本是基本要求。Windows PowerShell 中可以这样初始化：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-research.txt
```

也可以执行 `scripts/bootstrap_research_env.ps1` 做确定性的环境初始化。Agent 先执行：

```powershell
.\.venv\Scripts\python.exe run_review_harness.py --help
```

不要因为 `python` 指向了系统旧版本就继续跑；先确认 `python --version` 和实际调用的
`.venv\Scripts\python.exe` 是同一套环境。

### 6.2 PDF 环境

如果评委只检查研究文本，可以暂时没有 TeX；如果要求完整英文 PDF，建议安装并加入
`PATH`：

- Pandoc；
- TeX Live 或 MiKTeX，提供 `xelatex` 和 `latexmk`；
- Poppler，提供 `pdfinfo` 和 `pdftoppm`。

缺少这些工具时，程序仍可能产出 Markdown、bibliography 和 LaTeX 源文件，但不能把
“有 LaTeX 源文件”误报成“PDF 已生成”。需要严格验证 PDF 时加上 `--require-pdf`，让
缺失依赖成为明确的阻塞，而不是静默降级。

### 6.3 Qwen key 共享池

公开目录保留了以下相对路径布局：

```text
api_keys/
├─ qwen-api-key.txt
├─ semantic-scholar-api-key.txt
├─ openalex.txt
├─ core_api.txt
└─ Unpaywall.txt
```

实时模型运行的主要文件是 `api_keys/qwen-api-key.txt`。每行可以放一个 Qwen/DashScope
key；代码会把非空行作为同一个共享池，保留稳定的首选顺序，并在认证、配额、限流等
适合轮换的错误下切换候选。不要为“章节一”“视觉阶段”“正式测试”再创建一套不同的
文件名。

其余学术接口 key 是可选的；OpenAlex、Crossref、arXiv、Unpaywall 等有公开或本地回退
路径。没有 Qwen key 时，配置允许 mock 运行用于结构诊断，但那不是联网检索和 PDF 的
真实 E2E。

若需要明确隔离本次运行使用的 Qwen 文件，可使用：

```powershell
.\.venv\Scripts\python.exe run_review_harness.py `
  --question "你的研究问题" `
  --qwen-key-file api_keys/qwen-api-key.txt
```

不要从旧桌面文件、历史测试文件或多个目录自动拼接 key 池。除非明确需要诊断兼容性，
优先使用仓库内的 `api_keys/` 和 `--qwen-key-file`，这样成本和复现边界清楚。

### 6.4 推荐的真实综述启动命令

下面是当前主线的安全起点；它不主动设置各研究阶段的独立预算，只使用一个全局预算和
程序默认的阶段配置。视觉质量优先时可保留约 5 CNY 的视觉子预算：

```powershell
.\.venv\Scripts\python.exe run_review_harness.py `
  --question "写一篇关于规模化光子计算的文献综述，阐述从可编程集成光子芯片到 AI 加速与光互连" `
  --execution-profile private_study `
  --no-research-plan `
  --auto-confirm-query-plan `
  --visual-budget-cny 5 `
  --require-pdf `
  --output-root outputs/research_harness_e2e
```

说明：

- `--no-research-plan` 是当前用户要求的主线策略，不是异常退出开关；
- `--auto-confirm-query-plan` 只适合题面明确、需要无人值守的评测；如果题面含糊，
  应先人工检查 Query Planner 的计划和 `TOPIC_IDENTITY.json`；
- `--visual-budget-cny 5` 是质量优先的建议值，不是所有小预算运行都必须使用的固定值；
- 不要同时加 `--no-real-image-generation`，否则概念图自然不会生成；
- 不要随意加 `--phase3-llm-dag`；
- 不要为了“看起来更完整”强行设置 `--review-lead-budget-cny`、
  `--coverage-budget-cny`、`--authoring-budget-cny` 等阶段上限。阶段预算过小会把正常
  的下游恢复误判成失败；真正需要控制时先说明全局预算、视觉预算和预期交付。

如果只做离线入口检查，可以使用 `--preflight-only` 或 `--mock-query-planner`；这两种
模式都不能替代真实的检索—写作—PDF 验证。

## 7. 运行时如何观察和恢复

### 7.1 重点文件

每个运行目录应重点观察：

```text
HARNESS_STATE.json       阶段状态与恢复依据
HARNESS_COST.json        实际模型调用、Token 和成本账本
HARNESS_EVENTS.jsonl     追加式事件时间轴
HARNESS_METRICS.json     总耗时、阶段耗时和统计
HARNESS_RUN_REPORT.md    给人的简明运行报告
DELIVERY_GATE.json       交付门及降级原因
```

`HARNESS_COST.json` 中的预算预留不是实际消费；要区分预检上限、已消耗成本、剩余
预算和失败重试成本。不要把一次网络异常的预留费用当作已经支付。

### 7.2 断点恢复协议

进程中断、网络短暂失败或某个阶段需要重新启动时，优先用原运行目录恢复：

```powershell
.\.venv\Scripts\python.exe run_review_harness.py `
  --question "与原运行完全相同的研究问题" `
  --execution-profile private_study `
  --run-dir outputs/research_harness_e2e/<原运行目录>
```

恢复前先检查：

1. 题面与 `TOPIC_IDENTITY.json` 是否一致；
2. `HARNESS_STATE.json` 是否有最近完成的阶段；
3. `HARNESS_COST.json` 是否能解释剩余预算；
4. 目标阶段的 manifest、内容文件和引用映射是否完整；
5. 是否存在一个可恢复的 `last_valid_candidate` 或下游有效资产。

正常恢复会复用已验证 manifest 和缓存。只有明确确认主题知识库或 Phase 3 交接损坏时，
才考虑 `--rebuild-scoped-kb` 或 `--rebuild-phase3-handoff`；这类参数会归档并重建相应
资产，不应作为普通重试按钮。不要删除整个运行目录来“解决”状态问题。

### 7.3 常见状态的解释

| 状态/现象 | 含义 | Agent 应做什么 |
| --- | --- | --- |
| `needs_model_recovery` | Query Planner 或关键模型暂时不可用 | 检查 Qwen key、配额和模型梯度后恢复原运行 |
| `needs_query_plan_revision` | 计划没有稳定的科学对象 | 不要自动确认；修订题面或计划后再进入检索 |
| `semantic_drift_blocked` | 蓝图改变了研究对象 | 检查主题身份和历史缓存隔离，禁止继续写错题目 |
| `needs_more_literature` | 章节材料与主题不足够相关 | 补检索或修订范围，不要用不相关证据硬写 |
| `validation_failed` | 验证没有通过 | 先看第一次验证错误；三次相同错误后会断路 |
| `awaiting_human_review` | 产物已生成，但策略要求人工确认 | 不要把它改写成系统崩溃；按交付门和 PDF 验收 |
| `completed_with_warnings` | 主阶段完成，同时有非致命警告 | 区分英文主交付和附属层警告 |

## 8. 已踩过的坑和处理方法

### 坑 1：作者达到 `max_iters` 就被误判为整条链失败

历史记录确实会写入 `ExceedMaxIters`。作者循环和工具迭代计数不是同一个数字，而且
达到上限时可能已经有一个结构完整、证据合法的最后候选。当前链路会把最后一个有效
候选持久化到章节工作目录，并允许下游的章节资产加强器、全文交接和出版层继续工作。

正确排查顺序：

1. 查看 `authoring` 阶段的 `LAST_VALID_SECTION_POINTER.json`、
   `LAST_VALID_SECTION_STATE.json` 和 `LAST_VALID_SECTION_DRAFT_EN.md`（如果该阶段写出）；
2. 查看章节报告中的候选状态、证据 handle、引用标记和验证结果；
3. 查看下游 `publication_mainline/` 是否已接收该章节；
4. 只有没有任何最低有效候选时，才把它升级为章节失败；
5. 优先从断点恢复，不要从头重跑所有论文检索。

### 坑 2：风格治理报告说没有“提升”，就误以为没有运行

目前有两层相关机制：

- 全文风格收敛层会统计模板段首、`While`、`Building on`、重复前缀等指标，并按有限
  波次改写；
- 章节级 Qwen 审稿人和返修作者逐章工作，重点是段首第一句、连接方式和名词简称，
  审稿与返修分离。

如果 `enabled=true`、`reviewer_calls/reviser_calls` 大于 0、`changed=true`，而
`promotion_eligible=false`，通常表示“文字实际改了，但全局指标没有达到晋级阈值”。
处理方式是查看 before/after 指标和最终英文稿，而不是加回固定的 `While` 替换规则。

### 坑 3：静态回放打开 404 或页面空白

回放页面依赖相对 JSON 和 JavaScript 资源，不能直接双击 `file://` 打开。必须在仓库
根目录执行：

```powershell
python -m http.server 18876 --directory replay
```

然后打开：

```text
http://127.0.0.1:18876/index.html
```

不要同时把 `replay/` 作为服务器根目录、又在 URL 中重复写 `/replay/`；也不要把某个
单独运行目录当成站点根目录。出现 404 时先检查：

1. `replay/index.html` 是否存在；
2. 服务器的 `--directory` 是否就是 `replay`；
3. URL 是否是 `/index.html` 而不是 `/replay/index.html`；
4. 端口是否被旧服务器占用；
5. 浏览器控制台是否仍在使用旧缓存。

### 坑 4：视觉机会有未填充项，就误判视觉模块没接入

`unfilled_visual_opportunities` 是“需求没有形成最终图”的计数，不是“视觉模块没有
执行”的计数。可能原因包括：

- 结构策略不允许摘要、结论或某些位置放图；
- 来源图与目标章节不相关，被审查拒绝；
- 图像生成的预计费用超过剩余视觉预算；
- 图示规格生成成功，但没有可渲染的本地资产；
- 视觉模型调用失败，系统保留了可追踪的降级记录。

排查时先看 `VISUAL_COST_REPORT.json` 的 `vision_calls`、`diagram_spec_calls`、
`image_generation_calls`、`reserved_generation_cost_cny`、`generated_figures` 和
`unfilled_visual_opportunities`，再看 `FINAL_VISUAL_AUDIT_REPORT.json`。如果需要概念图，
检查是否误用了 `--no-real-image-generation`，并提高 `--visual-budget-cny`；不要把
没有文件、caption、anchor 和 provenance 的条目手动标成“已完成”。

### 坑 5：跨主题缓存造成论文或视觉资产串题

中央材料缓存是为了复用，不是为了把所有历史材料混成一个无边界语料库。每次运行应
确认主题身份、topic-scoped KB 和视觉 cache namespace 一致。不要把上一个题目的
`TOPIC_IDENTITY.json`、视觉审核结果、caption-only approval 或 section ledger 复制到
新题目下。若怀疑缓存污染，先做只读 manifest 对比，再决定是否用显式 rebuild 参数。

### 坑 6：论文身份已经明确，却被错误当成“无效引用”

最终引用至少满足以下一种身份确认即可：

- DOI；
- Semantic Scholar ID；
- 题名和年份等足以确认论文身份的元数据。

正文中的 `[XX]` 引用标识存在且 bibliography 能指向那篇论文，就已经是正常标准引用。
某些 `final_text_only` 身份记录没有章节/片段链路时，应记为“身份已解析、片段追踪待补”
的可追溯性备注；不要因为缺少 fragment chain 就删除正文引用，也不要凭空补造 DOI、
作者或论文片段。只有论文身份无法确认、引用标识断裂或元数据互相矛盾时，才升级为
引用质量问题。

### 坑 7：Qwen 认证、配额或免费层错误被处理成“换模型万能解决”

DashScope 的认证、配额、欠费、限流等错误会先尝试轮换 key；模型特定的免费层分配
错误则可能保留当前 key，沿着模型 fallback 梯度下降。Agent 应按这个顺序处理：

1. 确认 `api_keys/qwen-api-key.txt` 非空行是否存在；
2. 确认当前 key 是哪个共享池来源，只输出掩码或来源，不输出值；
3. 判断错误是 key/account 级、模型分配级还是网络级；
4. 让配置的模型梯度执行 fallback；
5. 恢复原运行目录并检查成本账本。

不要把所有 key 内容拼进提示词，也不要在日志中打印 HTTP headers。`config/secret_pool.py`
会把 key 拆分、去重并只在诊断需要时输出掩码/指纹。

### 坑 8：中文 PDF 有审计警告，被误报为英文 PDF 无效

中文翻译是附属层。它会保护 `[REF:...]`、数字、公式、URL 和单位，再做块级翻译和语义
审计；若审计有警告，整体运行可能是 degraded，但英文稿和英文 PDF 仍然可以有效。
英文验收至少检查：

```text
publication/latex/main.pdf
publication/latex/main.tex
publication/latex/references.bib
publication/latex/LATEX_BUILD_REPORT.json
publication/latex/PUBLICATION_INTEGRITY_AUDIT.json
```

### 坑 9：缺少作者元数据，却把编译结果冒充投稿版

如果没有真实作者、单位、邮箱等信息，LaTeX 仍可能编译成功，但状态会类似
`compiled_awaiting_metadata`。评测演示可以使用 `OptoMind` 作为作者占位标识，但不能
把它描述成真实个人作者，也不能因此声称已经达到投稿就绪状态。补充真实元数据时，
优先使用独立的 publication metadata JSON，不要修改正文科学内容。

## 9. 引用、视觉和全文编辑的不可越界规则

### 引用

- 不得为没有来源的数字、机制、性能比较或未来预测自动补造引用。
- DOI/S2/题名—年份身份已经确认时，正常 `[XX]` 标识可以保留。
- 元数据解析器可以补作者、年份、期刊和 DOI，但不能用模型想象值填空。
- 引用身份、正文标识、bibliography 三者要在最终 PDF 中一致。

### 视觉

- 来源图必须保留论文身份、来源路径、caption、图/片段身份和版权状态。
- 结构化数据图由本地确定性绘制；近似值必须披露为近似，不能伪装成统一条件排名。
- 概念生成图是解释性资产，不是科学证据；要保留 AI 辅助/生成标记。
- 一张图只有在文件、caption、anchor、provenance 和审查状态都合格时才是 renderable。

### 全文编辑

- 全文司令员负责结构、重复和边界，不应重写上游科学证据。
- 审稿人和返修作者分离；返修输入必须包含审稿意见和目标章节原文。
- 只允许有限的连接词、指代、转折、重复说明和术语澄清编辑自动落地。
- claim、citation、evidence、scope 的实质变化不能被普通润色自动应用。
- 如果修改前后的引用 marker 序列或论点绑定发生实质变化，应拒绝这次润色，保留
  原始有效稿和审稿意见。

## 10. 新运行的验收清单

Agent 完成一次新运行后，按以下顺序给出报告：

1. **题目和主题身份**：报告最终主题、章节数，并确认没有语义漂移。
2. **交付文件**：确认 `publication/latex/main.pdf` 存在且可读取；中文 PDF 单独报告，
   不要用中文警告覆盖英文结果。
3. **文章规模**：报告英文正文词数、章节数、引用数和 PDF 页数。
4. **调用投入**：报告模型调用类型、调用次数、输入/输出 Token、总成本和活跃时间；
   说明是否含重试，不要把工具调用冒充模型调用。
5. **引用质量**：抽查 DOI/S2/题名—年份身份、`[XX]` 标识、bibliography 和 PDF 引用。
6. **风格治理**：同时报告治理是否启用、reviewer/reviser 调用、实际变化和全局指标；
   `promotion_eligible=false` 不能单独作为“未运行”结论。
7. **视觉结果**：报告 renderable 数量、未填充机会、视觉预算、生成调用和最终审查状态。
8. **降级和错误**：区分可恢复错误、fail-open 保留的最后有效候选、附属翻译警告和
   真正阻断科学对象/证据的 fail-closed 状态。
9. **恢复性**：记录可以从哪个运行目录、哪个阶段和哪些 checkpoint 继续。
10. **结论边界**：明确这是自动化研究综述交付和工程验证，不是论文事实已经经过真人
    同行评审的证明。

三次历史运行的便携成果位于：

```text
artifacts/e2e/01-optical-diffractive-neural-networks/
artifacts/e2e/02-metasurface-holography/
artifacts/e2e/03-scalable-photonic-computing/
```

静态回放位于 `replay/`。运行时原始缓存、下载全文和本地数据库不应被 Agent 当成公开
成果层；公开成果应优先使用 `artifacts/e2e/` 中的 publication、audit、summary 和
PDF 文件。

## 11. 诚实但不夸大的限制

这些限制是当前版本的真实边界，不影响评委判断项目贡献，但不应被 Agent 隐瞒：

- 三次完整验证主要集中在光学/电磁学，跨学科迁移仍需重新验证检索覆盖和领域提示词。
- 文章可以自动生成和编译，科学正确性、重要性已经经过内部测试为合格，但是是否适合投稿仍需要领域专家审阅。
- 视觉规划可以记录多个未填充机会；预算、相关性和可渲染性优先于“每章强行放图”。
- 生成概念图的文字标签和科学关系仍需人工快速检查；概念图不能替代原始论文图表。
- 模型价格、联网接口、开放全文可用性和图像生成结果会随时间变化；历史成本是实测值，
  新机器不应承诺完全相同的金额和运行时间。

## 12. 给后续 Agent 的最后一句话

先确认题目身份，再确认材料相关性；先看 checkpoint 和成本账本，再决定是否重跑；先
验收英文 PDF、引用和审计，再评价整体状态；先分清“治理实际发生”和“全局指标晋级”，
再讨论风格模块是否有效。这个项目的价值在于把研究过程变成可观察、可恢复、可审计的
证据链，而不是让模型把所有不确定性隐藏在一篇看起来流畅的文章里。

## 13. 从协作记录补充的根因级故障地图

前面的章节主要告诉 Agent“看到某种现象怎么办”。这一节进一步说明“为什么会出现这种
现象”。如果只修表面报错，下一次运行仍然会重复消耗预算；正确的诊断顺序应该是先确认
语义层、状态层、预算层和文件边界，再决定是否需要修改代码。


根因是把三个不同层次压成了一个布尔值：

```text
论文身份确认       这条引用指向哪篇论文？
证据支持关系       这一章的哪一个主张由哪一个正文片段支持？
出版引用关系       正文中的 [XX] 是否能指向 bibliography 中的论文？
```

正确的 Agent 判断方式是：

1. DOI、S2 ID 或可靠的题名—年份组合已经确认时，论文身份层通过；
2. 正文有 `[XX]`，且 bibliography 能指向这篇论文时，出版引用层通过；
3. 章节/片段链路缺失时，记录为“身份已解析、片段追踪待补”的可追溯性备注；
4. 只有在论文身份不确定、`[XX]` 断裂、bibliography 指向另一篇论文，或某个科学主张
   没有任何允许的证据时，才升级为引用/证据失败。

不要因为一条 `final_text_only` 记录没有 fragment chain 就删除正文引用，也不要在最终
元数据阶段为了消除 warning 而编造 DOI、作者、片段或章节归属。证据不足应该在章节
coverage、论点卡和作者阶段被处理；身份确认应该由 publication metadata resolver 处理。

### 13.2 Commander 语法损坏和论文身份误判是两条根因链

曾经出现过“Commander 文件语法损坏”和“论文身份归并错误”同时暴露的情况。它们看起来
都可能在全文交接或最终引用阶段爆炸，但实际上属于不同层面：

- 语法损坏是文件/解析层故障，可能让错误延迟到几千行之后才显示；
- DOI/S2 归并错误是身份语义层故障，即使 Python 能导入，系统也可能把两篇相近论文
  当成一篇，或把一个历史论文身份注入当前题目。

Agent 的第一轮动作必须把两条链拆开：

```powershell
python -m py_compile optomind_research/runtime/global_manuscript_commander.py
python -m pytest -q tests/test_global_manuscript_commander.py tests/test_publication_metadata_resolver.py
```

如果语法检查失败，先修复最早的解析错误并重新导入，不能根据后面的引用错误猜测科学
问题。如果语法通过但身份审计失败，检查 DOI 规范化、S2 ID、题名、年份、作者和期刊
字段的合并依据；标题相似度本身不能成为把两篇论文合并的唯一理由。

身份归并的安全原则是：

- DOI 去掉 `doi:`/URL 前缀、首尾标点并统一大小写后比较；
- S2 ID 必须作为稳定身份字段使用，不能把普通搜索结果序号当成 S2 ID；
- 题名/年份只能作为回退确认，需要结合作者、期刊或其他元数据消歧；
- 身份冲突时宁可保留两个候选并记录冲突，也不要为了减少记录数量强行合并；
- 归并后仍要保留来源映射，不能只留下一个没有来源的“最佳论文”。

### 13.3 `max_iters`、ReAct 迭代和模型调用次数不是同一个计数器

作者阶段的“到达最大迭代次数”曾经被误读为“章节失败”。实际存在至少三种计数：

- AgentScope/ReAct 的循环迭代次数，例如 `react_iter`；
- 已完成的模型调用次数；
- 某个阶段自己的工具或验证次数。

它们不一定同步，所以日志中的 `max_iters=24` 不代表已经消耗了 24 次同类模型调用，
也不代表没有可用文本。当前章节作者会持久化最后一个最低有效候选，并允许下游章节资产
加强器继续工作。真正的根因是“终止状态的可观测性”和“最后有效资产的恢复协议”没有
被人正确解读，而不是每次达到上限都需要重写作者循环。

遇到该现象时，不要先删除章节、清空运行目录或重跑整条检索链。先查：

1. `LAST_VALID_SECTION_POINTER.json`；
2. `LAST_VALID_SECTION_STATE.json`；
3. `LAST_VALID_SECTION_DRAFT_EN.md`；
4. 章节引用 marker、证据 handle 和下游增强报告；
5. `HARNESS_COST.json` 中真实已消费金额，而不是预留金额。

只有没有最低有效候选、候选改变了科学对象、引用 marker 断裂或证据验证失败时，才把
它升级为章节阻断。否则应记录“达到迭代上限但保留最后有效候选”，然后从原运行目录恢复。

### 13.4 风格治理存在多个指标和多个执行层，不能用一个字段代表全部

风格问题的根因不是简单地把 `While` 换成另一个固定开头。当前系统同时存在全文风格
收敛、章节级 Qwen reviewer/reviser，以及更早的有限风格管线；它们的指标含义不同：

```text
enabled                 是否启用
changed                 是否产生过可接受修改
metrics_after           修改后的局部/全文统计
improved                某个评价器是否认为指标改善
promotion_eligible      是否达到晋级或提升主线的阈值
```

因此可能同时出现 `enabled=true`、`changed=true`、`improved=false` 和
`promotion_eligible=false`。这不是矛盾，而是“局部文字发生了改善，但全局收敛指标没有
达到系统提升门槛”。历史三次运行中，模板段首、`While` 和 `Building on` 的统计都曾
明显下降；这证明治理执行过，但不等于所有章节在所有指标上都达到理想值。

更深一层的根因是段首句对阅读体验的影响远大于普通句子。后续 Agent 不应只看整篇的
平均重复率，而要检查：

- 每个段落第一句是否重复使用同一语法骨架；
- 同一章相邻段落是否连续以 `While`、`Building on`、`This approach` 等方式开头；
- 简称第一次出现是否带全称，第二次以后是否稳定使用简称；
- 润色前后 claim、citation、evidence 和 scope 是否保持不变。

审稿人和返修作者必须保持角色分离。润色只允许处理段首、连接、指代、重复说明和不
改变科学含义的术语表达；一旦论点、引用或证据绑定发生实质变化，就拒绝这次润色，
而不是为了让风格分数好看而接收它。

### 13.5 独立阶段预算会和全局账本、视觉预留互相制造假失败

曾经的运行经验说明：给 review lead、coverage、authoring、publication 和 visual
分别设置很小的预算，会导致某一阶段提前停掉，即使整个运行仍有余额。视觉模块还会
在真正生成前预留图像成本；如果预留后余额不足，概念图会在生成前被跳过。

这类问题的根因是把“硬全局上限”“阶段内部停止条件”“视觉成本预留”“实际已消费
金额”混成一个数字。当前推荐的运行策略是：

- 普通 E2E 只设置全局预算，让阶段使用 profile 默认值；
- 质量优先时为视觉设置明确的独立子预算，约 5 CNY 是合理起点，但不能脱离全局预算；
- 不要用很小的阶段 cap 强迫作者在证据还没有完成时停止；
- 诊断预算失败时同时查看预检上限、已消费、预留、剩余和重试，不要只看一个
  `budget_exhausted` 字符串。

### 13.6 “视觉模块没接入”和“视觉需求没有形成图片”是两件事

视觉主线是“规划 → 契约 → 当前运行优先的候选检索 → 相关性审查 → 来源/数据/概念
图路由 → 生成或渲染 → 人工/系统审查 → LaTeX 挂载”。其中任何一个门都可能产生
未填充机会。根因分类应至少包括：

```text
planned                  规划过，但还没有进入物化
budget_skipped           预算预留不允许生成
structurally_excluded   位置策略不允许放图
relevance_rejected      候选图与章节/论点不相关
not_renderable           没有文件、caption、anchor 或 provenance
transport_failed         模型/网络失败，走降级记录
mounted                  真正进入最终出版层
```

不能把 `unfilled_visual_opportunities > 0` 直接判为模块未接入。历史运行中，第一次有
7 个未填充机会且 0 张最终图；第二次挂载 3 张概念图；第三次挂载 2 张概念图。差异来自
预算、候选和生成结果，不是入口代码是否存在。

视觉缓存也有根因级风险：旧的 caption-only approval 不能跨主题、跨章节或跨 prompt
复用；生成图缓存必须包含完整任务内容、主题命名空间和提示词指纹。怀疑串题时，先查
`cache_namespace`、目标章节和提示词 hash，再决定是否重建，不能直接复制上一个题目的
图片或审查记录。

### 13.7 静态回放的 404 是浏览器 origin 与资源根目录不一致

静态回放曾经反复出现 404/空白，这不是页面业务数据消失，而是三件事没有同时对齐：

1. HTML 使用相对资源地址；
2. JSON/JS 必须由 HTTP origin 提供；
3. HTTP server 的根目录必须正好是 `replay/`。

正确启动方式仍然是：

```powershell
python -m http.server 18876 --directory replay
```

访问 `/index.html`，而不是把 `/replay/` 再拼进 URL。若页面已经能打开但用户看到了内部
测试代号，根因则转移到了展示层映射：默认界面应把运行目录、阶段 ID、内部字段和状态
映射成“研究主题、文献检索、证据整理、章节写作、视觉处理、英文 PDF”等可懂名称；
原始字段只应放在诊断详情中，不应成为首页主文案。

### 13.8 可选研究计划、全局 DAG 和出版主线不能被 Agent 自行混跑

可选研究计划分支会增加阶段、翻译和出版产物；它不是当前综述主线的必要条件。评估
普通文献综述时使用 `--no-research-plan`，不要因为缺少研究计划文件而补跑另一条链。
同理，昂贵的全局 Phase 3 DAG 默认关闭；除非任务明确要求全局关系图，否则不要为了
“更像 Agent”而打开它。

当前默认出版主线是按章节生成可追踪草稿，再做全文完成和出版；不要擅自使用
`--no-publication-mainline` 回到旧的一次性文章完成路径。两条路径产物相似，但证据
绑定、风格治理、全文交接和恢复协议不同，混用会让 Agent 读错 checkpoint。

### 13.9 工作区边界错误会把缓存、历史产物和不相关支线混成一个项目

早期反复整理工作区时，脏文件夹、历史 E2E、下载全文、数据库、桌面 key 文件和并行
支线同时存在。根因不是“文件太多”本身，而是 Agent 没有先建立工作区边界，就用递归
扫描、全局搜索或宽泛复制命令处理整个父目录。

安全协议是：

- 先把当前仓库根目录作为唯一工作根；
- 输出、数据库、全文下载和缓存使用相对路径，且按主题隔离；
- 历史 E2E 的公开成果只读取 `artifacts/e2e/` 和 `replay/`；
- 不把 raw run tree 当成源代码，也不把缓存当成新题目的证据；
- 不触碰不属于当前任务的平行支线；
- 清理时优先归档和记录，不直接删除用户资产。

### 13.10 API key 的根因问题是“来源边界不清”，不只是文件名问题

把 key 从多个测试文件移动到 `api_keys/` 只是第一步。真正需要防止的是同一次运行同时
从环境变量、桌面旧文件、历史测试池和仓库文件读取，导致 Agent 不知道实际用了哪一个
账号，也无法解释费用和轮换行为。

公开包中的推荐边界是：

```text
api_keys/qwen-api-key.txt       Qwen/DashScope 共享池，每行一个 key
api_keys/semantic-scholar-api-key.txt
api_keys/openalex.txt
api_keys/core_api.txt
api_keys/Unpaywall.txt          可选学术接口凭据
```

实时运行前，优先显式指定 `--qwen-key-file api_keys/qwen-api-key.txt`，并确认非空行数量；
日志只能记录来源、序号、掩码或指纹。没有 Qwen key 时的 mock 模式只能验证结构，不能
冒充一次真实检索 E2E。


## 14. 根因优先的统一排查顺序

当一个新 Agent 报告“项目跑不通”时，按下面顺序排查，避免直接重跑和重复付费：

1. **文件完整性**：入口文件能否导入，最早语法错误在哪里，依赖版本是否正确。
2. **题目身份**：Query Planner 结果是否有效，`TOPIC_IDENTITY.json` 是否与题面一致。
3. **证据相关性**：当前材料、章节 coverage 和论点卡是否属于当前主题。
4. **工作区边界**：路径是否相对，缓存、数据库、历史运行和 key 池是否被串用。
5. **凭据与网络**：Qwen key 来源、账号配额、模型分配、开放全文和在线元数据接口。
6. **预算账本**：全局余额、阶段停止条件、视觉预留、重试费用和实际消费。
7. **恢复资产**：最后有效章节候选、阶段 manifest、状态文件、引用映射和交接文件。
8. **出版交付**：英文 Markdown、bibliography、LaTeX、PDF、视觉审计和引用完整性。
9. **展示层**：静态回放 server root、JSON/JS 加载、用户可读字段映射和 UI 状态。
10. **文档边界**：公开 README、Agent 手册、内部诊断记录和密钥模板是否混淆。

只有前一层通过后，才进入后一层。尤其不能在题目身份没有通过时继续花钱检索，也不
能在状态文件和最后有效候选尚未检查时删除运行目录。

---

透明说明：OptoMind-Review 的代码、测试、文档整理和部分运行分析使用了 ChatGPT、Qwen
以及其他 AI 辅助工具；本说明书本身面向后续协作的 AI Agent，不能替代人类对科学内容
和最终 PDF 的判断。
