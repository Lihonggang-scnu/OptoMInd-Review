# OptoMind Review 2：本地主链升级工作区

> **当前升级基线已于2026-09-29阶段冻结。** 后续Agent先读 [当前版本与历史版本说明](docs/REVIEW_V2_FROZEN_BASELINE.md)。当前推荐链路是 `local-upgrade` 中显式启用 `--planning-revision` 的实现；旧模式仅保留兼容，不是待选的平行方案。停止本轮规划/编排/写作小样调优，下一步转入完整综述成稿整合。冻结不等于免人工检查直接发表，已知问题见上述文档。下面的原始工作区与旧主链说明保留作历史背景，不得据此覆盖当前升级实现。

> **状态：比赛提交版本的本地升级副本，不是云端发布目录。**
>
> **禁止推送 GitHub。禁止推送 GitHub。禁止推送 GitHub。**

本目录从 `OptoMind-Review-1` 的研究与出版主链抽取而来，用于在不影响现有比赛提交仓、GitHub Pages 和提交材料超链接的前提下，继续进行本地升级、修复和性能优化。它是独立的私有工作区，不是新的公开发行版。

本目录内保存了真实 API 凭据，只能在本机使用。任何 Agent、脚本或人工操作都不得向 GitHub、其他代码托管站、公共聊天、日志服务或第三方文件服务上传本目录内容。

## 1. 当前基线是什么

源项目已经用三个不同的光学题目完成完整端到端验证：

1. 光学衍射神经网络；
2. 超表面全息：从逆向设计、制造到动态显示与成像应用；
3. 规模化光子计算：从可编程集成光子芯片到 AI 加速与光互连。

三次运行合计形成约 42,800 词英文正文、22 个章节、353 条最终引用和 117 页英文 PDF，记录约 943 次模型调用、27.764M Token、5 小时 40 分活跃运行时间和约 21.47 元调用成本。历史产物属于比赛提交与静态回放层，**本精简副本没有复制它们**。

当前可运行主链包含 13 个公开阶段：

```text
用户研究问题
  → 启动与配置
  → 问题理解与主题身份
  → 文献发现、开放全文和正文片段
  → 中央材料库与主题隔离知识库
  → 章节证据覆盖
  → 论点卡与生产交接
  → 章节初稿
  → 章节资产加强
  → 全文结构与一致性处理
  → 标题、结论、引言和摘要
  → 视觉规划、生成与挂载
  → 引用元数据解析
  → 英文/中文 LaTeX 与 PDF
```

英文 PDF 是主交付物；中文 PDF 是附属翻译资产。可选研究计划分支不属于当前普通综述主线，默认使用 `--no-research-plan`。昂贵的全局 Phase 3 关系图默认关闭。

## 2. 本副本包含什么

| 路径 | 用途 |
| --- | --- |
| `run_review_harness.py` | 唯一推荐的完整主链入口 |
| `config/` | 模型策略、价格、检索后端、密钥池和出版元数据配置 |
| `domain_config.yaml` | 领域词汇、检索和评价配置 |
| `llm/` | Qwen 调用、路由和模型响应处理 |
| `optomind_research/` | 检索、证据、论点、写作、视觉、引用和出版核心实现 |
| `prompts/` | 各阶段受版本控制的模型职责与输出契约 |
| `schemas/` | 主链结构定义 |
| `skills/` | 运行时加载的研究、写作、审计和视觉技能说明 |
| `tools/` | Semantic Scholar、OpenAlex、Crossref、Unpaywall、CORE、arXiv 等学术后端 |
| `scripts/` | 主链必需入口和阶段级恢复工具；已排除回放、演示和 smoke 脚本 |
| `api_keys/` | 本机真实 API 凭据，受 `.gitignore` 保护 |
| `outputs/` | 新运行的相对输出根目录，初始为空 |
| `data/`、`database/`、`literature_workspace/` | 新运行自行建立的缓存、数据库和全文工作区，初始为空 |

本副本有意排除了：

- `.git` 和原云端 remote；
- `artifacts/e2e/` 三次正式测试成果；
- `replay/` 和全部静态回放前端；
- `optomind_ui/` 与桌面应用；
- `tests/`、测试缓存、历史调试目录和临时探针；
- 旧 `outputs/`、`data/`、`database/`、`literature_workspace/`；
- 白皮书、DOCX、赛事材料、公开交付边界文档和一次性制图脚本；
- 与完整主链运行无关的说明材料。

因此，“没有三次历史 PDF”“没有回放页面”“没有网页门户”是这个升级副本的预期状态，不是复制失败。

## 3. GitHub 冻结政策

现有云端比赛产品继续由原仓库提供，本目录不负责部署。对本工作区的硬性规定是：

- 不添加 GitHub remote；
- 不执行任何形式的 `git push`；
- 不使用 `gh` 或 GitHub API 修改云端；
- 不修改原仓库权限、Pages、Actions、分支或发布；
- 不把本地升级误写成对现有云端版本的热修复；
- 不因为完成本地测试就自行发布。

本目录会建立一个**无远端的独立本地 Git 仓库**，用途只是保存本地基线、查看差异和回滚。它具有 `push.default=nothing`、GitHub 推送 URL 重写和 `pre-push` 拒绝钩子。即使如此，所有 Agent 仍必须遵守根目录 `AGENTS.md`，不得删除或绕过保护。

未来只有项目负责人明确给出一次具体发布批准后，才可以另行设计“审计—脱敏—合并—发布”流程。一般性的“继续”“修复完成”“做完了”不构成发布授权。

## 4. 推荐环境

### 4.1 Python

- 最低版本：Python 3.11；
- 当前本机验证环境：Python 3.12.4；
- 推荐在本目录创建独立 `.venv`，不要长期依赖其他项目的虚拟环境；
- Windows PowerShell 是当前主要运行环境。

标准安装：

```powershell
Set-Location <OptoMind-Review-2目录>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-research.txt
```

如果本机安装了 `uv`，也可以使用：

```powershell
.\scripts\bootstrap_research_env.ps1
```

当前 `pyproject.toml` 与 `requirements-research.txt` 存在若干版本口径差异，并且 `pyproject.toml` 尚未完整声明 PyYAML、openpyxl 等运行依赖。升级完成前，**以 `requirements-research.txt` 为环境安装依据**，不要只执行 `pip install -e .` 后就假定环境完整。

### 4.2 英文 PDF 工具链

需要完整 PDF 时，应在 `PATH` 中提供：

- Pandoc；
- TeX Live 或 MiKTeX；
- `xelatex`；
- `latexmk`；
- Poppler 的 `pdfinfo` 和 `pdftoppm`。

当前本机已检测到 Pandoc、TeX Live 2026、XeLaTeX、latexmk 和 Poppler。没有这些程序时仍可能生成 Markdown、BibTeX 和 LaTeX，但不能把它报告为“PDF 完成交付”。正式验收应使用 `--require-pdf`。

### 4.3 API 凭据

本目录的 `api_keys/` 来自本机真实密钥池。主要文件包括：

```text
api_keys/qwen-api-key.txt
api_keys/semantic-scholar-api-key.txt
api_keys/openalex.txt
api_keys/core_api.txt
api_keys/Unpaywall.txt
```

`qwen-api-key.txt` 每个非空行代表一个可轮换 Qwen/DashScope key。真实运行应显式指定：

```text
--qwen-key-file api_keys/qwen-api-key.txt
```

安全要求：

- 不打印 key 原文；
- 不把 key 写入模型提示词、异常、报告或事件日志；
- 不复制到 `.env`、桌面临时文件或新的测试池；
- 不把 `api_keys/` 加入任何提交；
- 最多报告文件存在、字节数、非空 key 数量、掩码或本地指纹；
- 认证、配额或限流错误应按共享池轮换规则处理，不要把全部 key 拼到一次请求里。

### 4.4 Qwen 长上下文与传输窗口

本项目保留 Qwen 的混合思考能力；长上下文审计、论点组织和研究判断不通过关闭思考来换取表面上的快速返回。Windows 环境默认优先使用系统 `curl.exe` 连接 DashScope，以绕开部分 Python/OpenSSL 版本在 TLS 重协商处提前断开的兼容性问题。需要较长时间的请求应放大传输窗口，而不是压低模型能力：

```powershell
$env:QWEN_HTTP_TRANSPORT = "auto"
$env:QWEN_HTTP_TIMEOUT_SEC = "300"
$env:QWEN_CLAIM_POOL_HTTP_TIMEOUT_SEC = "300"
$env:QWEN_EVIDENCE_VERIFIER_HTTP_TIMEOUT_SEC = "300"
```

其中 `QWEN_HTTP_TIMEOUT_SEC` 覆盖普通聊天、审计和补证请求，`QWEN_CLAIM_POOL_HTTP_TIMEOUT_SEC` 覆盖批量论点池请求，`QWEN_EVIDENCE_VERIFIER_HTTP_TIMEOUT_SEC` 覆盖论点—证据绑定请求；数值可按网络和上下文长度提高到 600 秒。`auto` 在 Windows 上选择可用的系统 curl；只有做传输诊断时才使用 `QWEN_HTTP_TRANSPORT=urllib`。扩大窗口不会关闭思考、不会改变章节证据边界，也不会把超时结果伪装成成功内容。

### 4.5 原始响应保留与断点重放

每次真实运行会在该运行目录的 `_llm_response_cache/` 中先保存 Qwen 的原始响应字节，再执行 JSON、证据和业务契约解析。缓存记录只保存请求指纹、模型、HTTP 状态和响应体，不保存 API key 原文；相同运行目录恢复时优先重放已收到的响应，因此修改解析器、契约或边界规则后无需再次请求、重复扣费。响应体不完整或无法解析时仍会保留为诊断材料，但不会自动当作成功内容；只有明确删除对应缓存并重新授权，才会重新请求。

## 5. 启动前检查

先确认入口能够导入：

```powershell
.\.venv\Scripts\python.exe run_review_harness.py --help
```

再做不调用模型的预算预检：

```powershell
.\.venv\Scripts\python.exe run_review_harness.py `
  --question "待运行的完整研究题面" `
  --execution-profile private_study `
  --no-research-plan `
  --qwen-key-file api_keys/qwen-api-key.txt `
  --preflight-only
```

`--preflight-only` 只写出并显示成本准入计划，不调用模型。`--mock-query-planner` 也只能用于离线结构检查，不能替代真实 E2E。

## 6. 正式运行方式

只有在题面、全局费用上限和实际启动得到明确授权后，才运行真实 E2E。推荐起点：

```powershell
.\.venv\Scripts\python.exe run_review_harness.py `
  --question "完整研究题面" `
  --execution-profile private_study `
  --no-research-plan `
  --auto-confirm-query-plan `
  --qwen-key-file api_keys/qwen-api-key.txt `
  --global-budget-cny <已批准的全局上限> `
  --visual-budget-cny 5 `
  --require-pdf `
  --output-root outputs/research_harness_e2e
```

关键规则：

- 不要额外设置很小的 review lead、coverage、authoring、publication 等阶段预算；它们可能在全局仍有余额时制造假失败；
- `--visual-budget-cny 5` 是质量优先时的合理起点，不是脱离全局预算的额外授权；
- 不要添加 `--no-real-image-generation`，否则概念图不会生成；
- 不要擅自添加 `--phase3-llm-dag`；
- 题面含糊时不要使用 `--auto-confirm-query-plan`，应先人工检查主题身份和查询计划；
- 作者占位信息可以使用 `OptoMind`，但不得冒充真实个人作者或已达到投稿就绪状态。

## 7. 运行观察与断点恢复

每次运行至少观察：

```text
HARNESS_STATE.json       阶段状态与恢复依据
HARNESS_COST.json        模型调用、Token、实际消费和预算余额
HARNESS_EVENTS.jsonl     追加式事件顺序
HARNESS_METRICS.json     总耗时和阶段耗时
HARNESS_RUN_REPORT.md    可读运行摘要
DELIVERY_GATE.json       交付门与降级原因
```

中断或可恢复错误后，不要删除运行目录。使用完全相同的题面和原目录恢复：

```powershell
.\.venv\Scripts\python.exe run_review_harness.py `
  --question "与原运行完全相同的研究题面" `
  --execution-profile private_study `
  --no-research-plan `
  --qwen-key-file api_keys/qwen-api-key.txt `
  --run-dir outputs/research_harness_e2e/<原运行目录>
```

恢复前依次确认：

1. 题面与 `TOPIC_IDENTITY.json` 一致；
2. `HARNESS_STATE.json` 能指出最近完成阶段；
3. `HARNESS_COST.json` 中的已消费、预留和余额能够解释；
4. 阶段 manifest、引用映射和交接文件完整；
5. 作者阶段是否存在最后有效候选；
6. 缓存命名空间与当前主题一致。

只有明确证明主题知识库或 Phase 3 交接损坏，才考虑 `--rebuild-scoped-kb` 或 `--rebuild-phase3-handoff`。它们不是普通重试按钮。

## 8. 已知问题与根因处理

### 8.1 Qwen 全部失败后的静默 mock 回退（已于 WO-02 修复）

生产模式（默认 `OPTOMIND_EXECUTION_MODE=production`）下，模型调用全部重试/密钥池轮换耗尽后会抛出分类异常
（`RetryExhaustedError` 等，见 `llm/execution_semantics.py`），不再返回 `mock_fallback` 空结果；无凭据时直接抛
`MissingCredentialError`，不再自动进入 mock。离线 fake 只在显式 `force_mock=True` 或
`OPTOMIND_EXECUTION_MODE=offline_test` 时产生，且 usage 记录带 `offline_fake=true`、`execution_mode=offline_test`
标记。`--preflight-only` 现在报告 `credentials_present` 与 `ready`，生产模式缺凭据时以退出码 2 失败且不读取密钥内容。

### 8.2 作者达到 `max_iters`

`max_iters=18/24` 不等于没有可用章节。先检查最后有效候选、章节引用、证据句柄和下游增强产物；满足最低结构与证据条件时允许 fail-open 继续。只有没有最低有效候选、主题漂移、引用断裂或证据失败时才阻断。

### 8.3 `final_text_only` 引用身份

论文有 DOI、Semantic Scholar ID，或可靠题名—年份身份，正文有 `[XX]` 且 bibliography 指向正确论文时，就是正常引用。没有章节/片段链路只是一条可追溯性备注，不得据此删除引用或伪造 DOI、作者和片段。

### 8.4 风格治理看起来“未提升”

章节级 Qwen 审稿人和返修作者可能已经实际修改文本，而 `promotion_eligible=false` 只表示全局指标没有达到晋级门槛。必须联合查看 `enabled`、调用次数、`changed`、before/after 指标和最终英文稿。后续优化重点是段首第一句、相邻段模板重复和简称复用，不能退化成固定字符串替换；任何科学论点或引用变化都应拒绝润色结果。

### 8.5 视觉需求未填充

未填充不等于视觉模块未接入。应区分预算跳过、结构排除、候选不相关、不可渲染、传输失败和最终挂载。概念图需要合理视觉预算，缓存必须包含主题、章节、完整任务和提示词指纹，不能跨题复用 caption-only 审批。

### 8.6 Commander 语法与论文身份归并

这是两条独立故障链。先做 Python 语法和导入检查，再检查 DOI 规范化、S2 ID、题名、年份、作者和期刊的消歧。身份冲突时保留多个候选并记录冲突，不得为了减少记录强行归并。

### 8.7 缓存和工作区串题

每次新题必须重新确认主题身份、主题范围知识库和视觉缓存命名空间。不得复制上一次运行的 `TOPIC_IDENTITY.json`、章节账本、视觉审批或引用映射。怀疑污染时先只读比较 manifest，再决定是否重建。

### 8.8 依赖与性能

目前 `pyproject.toml` 和 `requirements-research.txt` 尚未统一；写作主循环存在串行瓶颈；运行状态更新会重复扫描日志；部分超大编排文件和缓存/状态实现仍需拆分。升级应先补回归保护，再做有边界的并发、增量索引和依赖单一事实来源，避免一次性重写主链。

## 9. 本地升级优先级

建议按以下顺序推进，每一步单独提交并验证：

1. 修复生产路径静默 mock 回退，保证真实 E2E 失败可见；
2. 对齐 `pyproject.toml` 与 `requirements-research.txt`，补齐 PyYAML、openpyxl 等依赖；
3. 为章节写作增加 2—3 个有界并发 worker，同章节工作保持串行；
4. 把全量日志索引改为增量 offset 解析；
5. 收敛阶段状态、缓存 TTL、原子写和合法迁移校验；
6. 抽象模型客户端协议，减少对单一调用实现和函数内联 import 的耦合；
7. 在不改变语义的前提下拆分巨型编排文件；
8. 加强章节风格治理、视觉缓存隔离和候选相关性；
9. 为每项升级补最小可复现测试与回归测试；
10. 最后再决定是否引入新的本地交互界面；本副本当前不包含 UI。

## 10. 每次修改后的最低验收

至少完成：

1. 全部 Python 文件通过语法解析；
2. `run_review_harness.py --help` 成功；
3. 修改模块可以从本目录导入；
4. `--preflight-only` 不调用模型；
5. 不读取历史 E2E 资产；
6. `api_keys/` 未进入 Git 状态；
7. `outputs/`、数据库和缓存未进入 Git 状态；
8. Git 没有 remote，推送钩子仍然拒绝；
9. 没有任何 GitHub API、Pages 或远端仓库写操作；
10. 对引用、风格、视觉或状态机的修改保留原来的证据与恢复边界。

完成真实运行后，还要单独核验英文 PDF、引用身份、bibliography、页面文本提取、模型调用次数、输入/输出 Token、实际费用、墙钟时间、风格治理、视觉挂载和所有降级事件。中文 PDF 警告不得直接覆盖英文主交付结论。

## 11. 给后续本地 Agent 的起点

1. 阅读 `AGENTS.md` 和本文件；
2. 确认当前目录没有 GitHub remote；
3. 只检查密钥文件存在性，不输出内容；
4. 使用独立 `.venv`；
5. 先运行入口与语法检查；
6. 只处理一个明确升级目标；
7. 先复现，再修改，再回归；
8. 只做本地提交；
9. 不启动未经授权的付费 E2E；
10. 永远不要自行发布。

这个工作区的目的不是改写已经公开的比赛历史，而是在冻结云端产品的同时，稳妥地演进下一版研究—证据—综述—出版主链。
