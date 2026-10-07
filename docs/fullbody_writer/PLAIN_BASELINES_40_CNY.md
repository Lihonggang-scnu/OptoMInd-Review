# 本地开工指令：完整 BODY 朴素基线，本轮总额40 CNY

更新：2026-10-07。请把本文件作为本轮后续本地执行的当前交接。旧 `LOCAL_AGENT_HANDOFF_100_CNY.md` 及之前交付的100元附件均已过时，其100元预算已被用户降为40元；这40元是**同一轮全文写作实验的总额**，涵盖已做和待做的高级路线、三条新基线、失败、重试、预留及不确定占额。不是新增40元，不允许重建账本清零。云端本次只做实现与离线验证，不调用付费模型。

## 1. 任务与不能改变的范围

用户已有批准后的全篇细纲、最终章节编排和完整可用科研材料。现在要检验：普通学术写作指令能写成什么样；独立完整章节直接拼接是什么效果；再加一次真正的全篇统稿是否值得。这是务实的产品质量比较，不做路线×模型×配置的全因子实验，不预设高级机制一定更好。

所有候选的产品和比较单位均为同一批准范围的**全部 BODY**。不得回到单章试验，不得将一个章节改名为 `FULL_BODY.md`，不得因经费或上下文不足删章、删材料、缩短后半篇或另造精简计划。不重跑上游规划、材料采集、精读、案例模块或首尾模块，不把人工发现的本题正确答案塞入候选提示。

优先保留和复用本轮已有真实高级稿、账本、原始响应和失败记录；历史单章试验只能作背景，不能替代完整 BODY 结果。旧稿若输入版本不同，可作有边界的参考，但不能伪称同输入对照。不同实验确实独立的历史账本不要擅自迁移；当前全文实验已经发生的所有费用必须在40元范围内核清。

## 2. 三条新路线实际做什么

### `plain_whole`：普通全文作者

使用短的 `plain_writer.md` 加 `plain_whole.md`，给模型相同的文章要求、完整原细纲、全部任务和对应完整可用来源材料，一次写出全篇 BODY。它保留基本科学准确性、引用与完整交付要求，不携带高级路线的长篇文风指导、跨领域写法示例、独立读者诊断或特殊答案。默认 Plus；不是有意降低质量的“劣质对照”。

全文请求在发送前估容量。若真实完整输入或输出要求不能容纳，保留明确的容量阻断记录；不要为了凑一个结果压缩材料或改用章节串行却仍标为 `plain_whole`。

### `chapter_concat`：各章独立写完，程序直接拼接

通常按批准章序为每章发起独立作者调用。每次给完整原始全文细纲、当前完整任务与对应完整材料，**不给实际已写前文**。仅当实际容量不能容纳整章时，才按完整任务边界拆成可容纳的组，保留全部组及各自完整材料，并在阶段记录中明确容量原因。不传前一章或前一组摘要来冒充独立，也不背地调用编辑器或连续作者。用确定性规则按批准章节和任务顺序拼接；本地拼接不收费。

只有全部批准章节和任务均完成，才得到完整 `chapter_concat` BODY。若连一个完整任务及其完整材料都不能容纳，明确停下；不截任务、不裁材料，也不为了控制文风任意设置小窗口。`current_task_ids` 是每次实际范围，章 ID 只用于定位；容量分组时不能要求每组都重写整章或重复章标题。最终文章可能存在跨章重复或衔接生硬，这些是要观察的实际结果；不得先人工修好再当自主基线评分。报告实际是一章一调用还是发生容量分组，不把容量分组隐藏为严格的一章一作者。

### `hierarchical_full`：同一拼接底稿，再做真实全文统稿

第一阶段就是与 `chapter_concat` 相同的独立完整章节写作。第二阶段给 reviser **整篇原稿原文、完整批准细纲、全部任务及全部相关来源材料**，要求输出编辑后的完整替换 BODY。它不是只读章节摘要，不是只给编辑意见，不是局部补丁，也不替换成高级读者驱动机制。

优先通过 `--draft path/to/chapter_concat/FULL_BODY_RESULT.json` 复用已完成拼接稿。CLI 核对文件名、完成状态、正文哈希、同一输入哈希、`chapter_concat` 路线身份；真实付费统稿只能复用 live 底稿。不能拿另一条高级路线稿件冒充本基线的独立章节底稿。未传 `--draft` 时此路线会自行生成各章，已有底稿时不要走这个较贵的入口。

运行中保存 `INDEPENDENT_FULL_BODY.md` 与 `INDEPENDENT_FULL_BODY_RESULT.json`。这份完整章节拼接稿可供续跑复用；`independent_draft_result_path` 和 `independent_draft_body_path` 指向实际运行的不可变副本，输出根目录另有便利副本。若统稿请求超容量、失败、输出截断或仍有未完成任务，保存原拼接稿，报告“底稿完整，全文统稿未完成”，不要把它标成完成的 `hierarchical_full`。完整底稿但统稿受阻时，`body_complete=true`、`pending_task_ids=[]` 仍可与 `complete=false`、`integration_pending=true` 同时出现；应检查 `integration_status` 和 `required_action`，不能只看任务清空。只有编辑器确实返回整篇完整 BODY 才完成该路线。

## 3. 先核清40元共享账本，再考虑付费

先找到**本轮正在使用的原账本**，核对是否存在运行中、已发送未收尾或 uncertain 调用。不要另起模型进程。降低上限不会撤回已发送请求：先停止旧脚本继续派发，保存所有已返回内容、usage 和在途状态，再降额。不得凭没有拿到结果就将 uncertain 释放为零费用。

若原账本为100元，使用以下工具。第一次只预览，核对路径、原上限和占用；第二次显式应用。该工具保留全部 reservation 和结算记录，写入降额审计记录，不会新增模型调用。

```bash
python scripts/upgrade3/lower_budget_limit.py \
  --ledger path/to/THIS_CAMPAIGN/budget.sqlite \
  --lower-to 40 --expected-limit 100 \
  --report path/to/THIS_CAMPAIGN/cap_preview_40.json

python scripts/upgrade3/lower_budget_limit.py \
  --ledger path/to/THIS_CAMPAIGN/budget.sqlite \
  --lower-to 40 --expected-limit 100 --apply \
  --report path/to/THIS_CAMPAIGN/cap_applied_40.json
```

原上限不是100时，先不带 `--expected-limit` 做只读预览，核实返回的 `old_limit_cny`，再用该实值作为 `--expected-limit` 应用。已经40元可直接沿用，不能再加40。工具只支持降额，不是隐性增额入口。若现有上限低于40，保留较低上限并报告，不通过新账本绕过。

如果本轮从未开始、确实无既有调用或账本，才可第一次用下方命令建立一个40元共享账本。高级路线和三条基线必须传相同路径；任何失败、模型分支和重试也使用它。历史输出目录名即便含 `100cny`，也不是必须迁移或新建账本的理由。

总占用 = 已结算费用 + 仍预留费用 + 不确定占额。剩余额度 = 40 − 总占用，不足零时按零处理并停止新调用。降额后总占用已经超过40，可以保留原账并收紧上限，但不能倒改已有花费来声称合规。剩余资金不足安全预留时保留结果、报告未做部分，不通过降低科学范围、绕过预留或分散账本取得“全部完成”。

四份现有配置不变：默认 `plus_first.json`，作者/读者/编辑均 Plus；`economy_flash.json`、`plus_reasoning.json`、`selective_max.json` 仅为有理由的可选分支。不要因为它们存在而重跑全部组合。Max 不自动升级，只有明确选取且带 `--allow-max` 才可付费执行。`hierarchical_full --draft` 只执行 reviser；未复用时执行 writer 与 reviser；这与独立读者 `reader_revision` 的 reader/reviser 不同。

## 4. 锁定相同完整输入，先跑离线检查

在仓库根目录使用已验证的本地 Python 环境。先看本轮已有 `SOURCE_MANIFEST.json`、已准备的源清单与全部批准章序。输入必须是本地完整原件；公开验收目录可能删去了受版权限制的材料，不能直接充当 live 输入。

复用正式 manifest loader、真实材料视图构建、稳定来源身份与哈希流程。它支持明确全篇清单和批准计划或 `CURRENT_PLAN.json`；不得用 glob 文件顺序反推批准范围。相同章数不代表同一输入，最终编排、材料和任务都要匹配。无需人工为每个文件计算哈希。

```bash
python scripts/upgrade3/fullbody_writer.py --help
python -m pytest -q tests/upgrade3/test_fullbody_contracts.py tests/upgrade3/test_fullbody_engine.py tests/upgrade3/test_fullbody_cli.py tests/upgrade3/test_fullbody_integration.py tests/upgrade3/test_fullbody_plain_routes.py tests/upgrade3/test_fullbody_plain_cli.py tests/upgrade3/test_fullbody_plain_integration.py tests/upgrade3/test_lower_budget_limit.py

# 仅在本轮尚无已准备的同输入清单时执行；输出必须是新文件。
python scripts/upgrade3/fullbody_writer.py \
  --manifest path/to/approved/BODY_MANIFEST.json \
  --plan path/to/approved/CURRENT_PLAN.json \
  --prepare-manifest path/to/THIS_CAMPAIGN/SOURCE_INPUTS.json
```

已有哈希锁定清单就直接复用，不重复追逐更新中的目录指针，不覆盖旧清单。准备后依赖变化应停下核对，必要时明确新版本和新输出目录；不能为了命中旧缓存而改哈希或拼混不同版本。保留原始用户要求、范围、语言和目标读者，以及正文全部原始纲要。

离线 preview 不读取密钥、不建付费 transport；`--responses` 只重放记录，也不收费。它们验证工程行为，不能作为新 live 文稿或科学质量结论。缺 tokenizer 时的字节上界很保守，可能误判容量；优先指定已有本地 tokenizer，不自动下载，也不因粗估太大就删材料。

## 5. 推荐执行顺序与可复制命令

先设置下列变量为本地真实路径。`CAMPAIGN` 和 `LEDGER` 必须指向**同一轮既有实验**；在已有结果时不要新建同额账本。保留路径中的引号，不把密钥内容写入参数、报告或消息。

```bash
CAMPAIGN="path/to/THIS_CAMPAIGN"
INPUT="$CAMPAIGN/SOURCE_INPUTS.json"
LEDGER="$CAMPAIGN/budget.sqlite"
CONFIG="config/fullbody_writer/plus_first.json"
TOKENIZER="path/to/existing/local/tokenizer.json"
KEYFILE="path/to/existing/local/key_file"
```

这些是 Bash 示例；Windows PowerShell 可按本地语法设置变量，参数语义相同。各候选用独立输出目录。下面是分步命令，**不是无需判断的一键全部付费脚本**。

### A. 普通全文：容量允许才做一次真实成稿

```bash
python scripts/upgrade3/fullbody_writer.py \
  --manifest "$INPUT" --route plain_whole --config "$CONFIG" \
  --tokenizer "$TOKENIZER" --output "$CAMPAIGN/plain_whole"
```

检查预览的 `RUN_MANIFEST.json`、阶段容量估算及实际 `MESSAGES.json`。确认全篇细纲与材料完整、请求能容纳、剩余额度能覆盖预留，且本轮尚无可复用的该结果后，才运行：

```bash
python scripts/upgrade3/fullbody_writer.py \
  --manifest "$INPUT" --route plain_whole --config "$CONFIG" \
  --tokenizer "$TOKENIZER" --output "$CAMPAIGN/plain_whole" \
  --run --budget-ledger "$LEDGER" --budget-limit 40 --key-file "$KEYFILE"
```

容量不合适时保存原因，转下一条基线，不把小稿塞进本路线。容量上限不是必须耗满的输出配额；不强迫词数、段数或引用数。

### B. 各章独立写作，拼接完整 BODY

```bash
python scripts/upgrade3/fullbody_writer.py \
  --manifest "$INPUT" --route chapter_concat --config "$CONFIG" \
  --tokenizer "$TOKENIZER" --output "$CAMPAIGN/chapter_concat"

# 核对完整输入、全部章序和预算后再执行。
python scripts/upgrade3/fullbody_writer.py \
  --manifest "$INPUT" --route chapter_concat --config "$CONFIG" \
  --tokenizer "$TOKENIZER" --output "$CAMPAIGN/chapter_concat" \
  --run --budget-ledger "$LEDGER" --budget-limit 40 --key-file "$KEYFILE"
```

检查每个 `chapter_001`、`chapter_002` 等阶段的请求，确认后续调用没有前章/前组真实正文或高级长写作提示。核对实际完整任务范围及容量分组原因；阶段序号在分组时不等同于章节号。章数和章序来自实际批准范围，上面的阶段名不是“两章实验”的授权。完成后查看完整 `FULL_BODY.md` 和 `FULL_BODY_RESULT.json`；只有全部任务完成的 live 拼接稿才可进入付费 C。

### C. 复用 B，只为全文统稿增量付费

```bash
DRAFT="$CAMPAIGN/chapter_concat/FULL_BODY_RESULT.json"

python scripts/upgrade3/fullbody_writer.py \
  --manifest "$INPUT" --route hierarchical_full --draft "$DRAFT" \
  --config "$CONFIG" --tokenizer "$TOKENIZER" \
  --output "$CAMPAIGN/hierarchical_full"

# 只有完整原稿＋全篇计划＋全部来源同时容纳且预算允许时执行。
python scripts/upgrade3/fullbody_writer.py \
  --manifest "$INPUT" --route hierarchical_full --draft "$DRAFT" \
  --config "$CONFIG" --tokenizer "$TOKENIZER" \
  --output "$CAMPAIGN/hierarchical_full" \
  --run --budget-ledger "$LEDGER" --budget-limit 40 --key-file "$KEYFILE"
```

`integrate_full_body` 阶段才是真正统稿。核对 `original_body_markdown` 与 B 正文哈希完全对应，并确认完整原细纲和全部来源实际在消息中。没有底稿时，预览不能假装知道统稿输入或精确统稿费用。若曾在 C 内完成独立章节后统稿受阻，可将 `DRAFT` 指向其已保存的 `INDEPENDENT_FULL_BODY_RESULT.json`，继续核查和复用。

若全篇统稿装不下，明确报告完整原拼接稿仍可用、C 待完成；不能悄悄改成摘要统稿、删材料或局部 patch，却继续使用同一基线名。读者驱动局部修订是另一条 `reader_revision` 路线，如要试必须另记且仍受本轮同一40元总额约束。

## 6. 恢复与费用：已经付过的钱不能再付一遍

保留输出、raw、usage、实际参数、attempt、源码版本和账本。完全相同的成功阶段恢复会命中缓存；输入、提示或实际代码依赖改变时不保证继续复用，先查看阶段缓存关系，不要仅因目录相同便认为免费。**本次新增基线和共享预算修复会改变代码依赖哈希；已有完整高级结果应直接作为比较材料，不要为了新版本缓存重新付费跑四条高级路线。** 跨路线优先用明确 `--draft`，不要重新生成所有章节。

- 正常重复同一命令复用成功阶段；成功阶段无需 `--retry-failed`
- 失败/uncertain 默认不自动重复；先确认是否仍在途、能否从已存完整 raw 离线恢复，再决定显式 `--retry-failed`。重复调用仍计入40元
- 输出截断只能如实标未完成，不能把 sidecar 改成 true 来升级。三条朴素基线会明确拒绝 `--continue-incomplete`；需要真实前缀续写时应另记 `continuous_author` / `workbench`，不能偷换本基线机制
- 真实编辑失败时保留完整拼接底稿和未完成状态；不要覆盖原稿，也不要以旧成功稿存在掩盖本次失败
- 遇到退出码3，检查当前尝试的 `CLI_RUN.json` 与 `RUN_MANIFEST.json`，它表示正式执行未完成；预览退出0不等于真实稿件完成。已有选用稿与当前尝试分开看

费用报告采用两个口径：

1. **本轮实际支出与占用：**所有高级与朴素路线的真实调用只在共享账本记一次，含废弃稿、失败、预留和 uncertain；不会因为 B 被 C 引用而二次扣费
2. **完整方案成本：**B 的独立各章成本；C 的同一底稿成本＋统稿增量。底稿复用不意味着 C 的完整方案成本只有编辑费，也不能把同一底稿重复加进本轮支出

底稿、当前新增调用、缓存复用与方案累计成本关系应能从 `cost_summary`、`stage_lineage`、`RUN_MANIFEST.json` 和原账本追溯。未知成本明确 unknown，不能写零。实际 provider 缓存命中才记缓存节省；不要假设重复的完整细纲和材料免费。

## 7. 阅读完整结果，回传能支持决策的证据

先验证全部批准章序、任务和正文真实完成，后半篇没有提纲化或截断，引用身份与图表正确。完成声明只是机器可检的覆盖信息，不证明科学质量。随后由本地 Agent 完整通读全部实际稿件并给出判断，结合来源核对关键主张；不能只抽首章或统计字数便宣布优胜，不把主观质量判断任务转交回用户。

比较朴素稿和已有高级稿时，重点看：材料支持的关系与机制是否真正展开，重要条件是否改变相应结论，论证能否贯穿全部章节，重复是否带来新认识，正文与表格是否一致，语言是否自然清楚。记录真实文本和来源证据；不设每章问题数、引用数、表格数或固定评分模板，不用人工正确答案诱导下一候选。

特别看 C 是否真的改善 B 的接缝、重复和一致性，是否保留有价值的科学内容，有没有为统稿而压短全文或引入新错误。允许结论是普通提示更好、拼接已经够用、编辑增益不值成本，或当前证据尚不足判断；不强迫高级机制获胜。

本地交付至少包括：

1. 源码版本、已执行离线测试结果、同一完整输入清单和哈希、批准章序，以及实际运行配置
2. 每条实际尝试路线的完整 live BODY，或准确的未完成/容量阻断原因；preview、replay、历史单章和 live 分开标注
3. C 的底稿路径/哈希、编辑前后完整稿与真实差异；若统稿未完成，明确保留的是哪份完整拼接稿
4. 请求消息、原始响应、实际模型/思考与回答预算、provider usage、缓存/恢复/attempt 与底稿复用关系
5. 原共享账本、40元降额前后记录、已结算/仍预留/uncertain/剩余，以及本轮实际支出和完整方案成本两种口径
6. 基于完整阅读的简洁判断：哪条路线当前最有用、代价是什么、哪些仍未证实；不要求为了完整矩阵再重跑已有高级路线

达到预算、容量确实不适用、未确定调用未核清，或继续需要改变范围/额外授权时就保留结果并报告。不要为填满路线表偷偷超支或降低成稿范围。
