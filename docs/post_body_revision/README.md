# OptoMind 独立正文修订实验模块

本模块实现三个可独立运行的候选方案。它读取固定正文、实际细纲与材料，产出局部修订候选及过程记录；原 BODY 规划、编排、写作与交付链均不改动。云端只做离线检查，真实模型实验由本地在明确预算下执行。

## 三个方案是同一底座的不同运行路径

| 方案 | 已实现路径 | 适合检验的问题 |
|---|---|---|
| A | 一次综合审稿同时提补丁，随后独立核验 | 少一个局部作者环节能否降低成本，代价是否是更多错误修改或保守不改 |
| B | 审稿列问题，读取所选完整材料，局部作者修订，独立核验 | 将问题发现与材料支持的修改分开，能否减少误删和错误归因 |
| C | B，加一次有界的较强模型作者与核验升级 | 只为有材料、首次核验未通过的实质问题付出更多费用，是否比普遍升级划算 |

A/B/C 使用同一个原始 case，互不读取对方的候选文本。方案名不意味着质量高低。C 只有科学或缺失内容任务在正常核验拒绝/不改后才升级一次；缺材料、无效定位、解析/传输错误不触发付费重试。每个方案都可以合法返回不修改。

默认模型配置使用仓库现有直接客户端支持的 `qwen3.7-flash`；C 的升级角色示例为 `qwen3.5-plus`。这是可执行配置，不是已验证的强弱排序。当前适配器不接受未知模型以免没有可信计费规则；更换支持范围需要单独核对价格和客户端，不会暗中降级模型。

## 文件与职责

- `optomind_research/runtime/upgrade3/post_body_revision_contracts.py`：固定输入、原文定位、证据引用、补丁约束与原样应用
- `post_body_revision.py`：共用 A/B/C 流程、逐次调用记录、恢复、费用与未决状态
- `post_body_revision_evaluation.py`：盲评输入、评价校验、解盲与有条件的成本质量比较
- `scripts/upgrade3/post_body_revision.py`：准备输入、零调用预览、录制回放、显式付费运行
- `scripts/upgrade3/evaluate_post_body_revision.py`：准备盲评包、消费本地 Agent 的评价
- `config/post_body_revision/A.json`、`B.json`、`C.json`：三个独立实验配置
- `prompts/post_body_revision/`：通用审稿、综合审稿、作者、核验与独立评估说明

复用了现有论文引用识别、直接模型客户端、预算账本及局部编辑/逐项恢复思路。没有把旧的首尾或完整 staged planner 接入，也没有把旧“事实数字全部不得变化”的修辞编辑合同冒充科学纠错。

## 输入和证据边界

最小 case 是 JSON，包含 `draft_text` 或 `draft_file`、`research_question`、`scope`、`outline`、`materials`、`source_identity_map`。`materials` 以材料 ID 为键，每项有 `text`、`title`、可选 `summary`、`source_handles`。材料 ID 用于取材与审计，论文句柄用于引用，不得混用。

`prepare` 可从 BODY 和当前最终 PLAN（JSON/gzip JSON）或显式材料清单生成 case。它只解析已知项目字段，不跟随 locator 去读文件，不读数据库，不联网搜索，不重新规划：

- 同时保留 shared_scope 和 review_argument
- 保留 shared_outline 与已知章节合同投影：thesis、reader_objective、units、原段落任务及条件/综合/衔接
- 读取已有 A/B、精读和补充/工具内容；不同材料快照分开保留，相同内容去重
- 合并互补身份元数据；明确相冲突的论文身份失败，不伪造一致性
- 记录投影规则和输入哈希。不能识别的材料格式明确报错，要求提供显式材料清单

默认审稿消息包含正文 snapshot blocks、范围、细纲和材料导航索引；它不是全池精读。A 在此有限信息上提候选补丁，所以更适合轻量编辑；B/C 的局部作者与所有方案的核验会读所选实际材料。材料索引不能充当科学证据。旧材料里已有的错配、过时主张不会被程序自动修正。

正文按原始 UTF-8 字节解码，保留 CRLF，不进行整稿格式重排。block ID 只对当前正文版本有效；换正文后旧问题和补丁不能继续套用。当前定位是 block 加逐字原文，不是对标题进行学科或职责分类。

## 问题与补丁合同

审稿问题包含 issue_id、kind（editorial/scientific/missing）、target_block_id、original_text、problem、evidence_ids、preserve、operation、priority。操作支持 replace、remove、insert_after。跨章节重复或矛盾可用可选 `related_block_ids` 指向当前正文中的实际对应段落；作者和核验读取这些原文，不能只相信审稿描述。`preserve` 如非空，必须是原文中的逐字保护片段；语义层面的机制、条件、阴性结果等保存责任始终由核验承担，不靠关键词判断。

科学修正与补缺必须有实际非空材料。程序验证引用身份和位置，不声称自动证明科学蕴含。语言编辑禁止引入新的数值/论文引用；它可以换词表达，不用固定词表限制学术写作。需要改数值或科学判断时应走 scientific 类型。

补丁以固定正文哈希、block 哈希、原文及位置绑定；重复定位、旧版本、重叠或不受支持的新引用不能直接应用。先形成独立 candidate，baseline 不覆盖。未触及的原文字节保持；被修改范围内允许必要变化。多处编辑从同一个原始 snapshot 计算，不按前一补丁重新猜位置。如果一个补丁会改动另一个补丁声明依赖的相关 block，后续任务保持未决，不把两次针对旧上下文的核验误当成联合核验；共享未修改的相关段落则允许。联合多段语义事务暂不支持，也没有全稿自动重写。

## 快速离线运行

在仓库根目录、已安装项目依赖的 Python 环境下：

```bash
python scripts/upgrade3/post_body_revision.py run --case tests/fixtures/post_body_revision/case.json --variant A --output-root outputs/post_body_revision_demo
python scripts/upgrade3/post_body_revision.py run --case tests/fixtures/post_body_revision/case.json --variant A --recordings tests/fixtures/post_body_revision/recordings.json --output-root outputs/post_body_revision_demo
python scripts/upgrade3/post_body_revision.py run --case tests/fixtures/post_body_revision/case.json --variant B --recordings tests/fixtures/post_body_revision/recordings.json --output-root outputs/post_body_revision_demo
python scripts/upgrade3/post_body_revision.py run --case tests/fixtures/post_body_revision/case.json --variant C --recordings tests/fixtures/post_body_revision/recordings_escalation.json --output-root outputs/post_body_revision_demo
```

第一条仅 preview，后面三个使用明确标记的虚构录制结果。C 的专用录制先拒绝再升级，实际覆盖五次调用边界；A/B 单问题样例分别覆盖两/三次。录制不是科学质量实验，不能进入真实成本质量结论。

输出分别进入 `A/preview`、`A/recording`、`B/recording`、`C/recording`。所有方案可以单独运行；默认不并发启动付费调用。没有录制的调用会停为未决/失败，不偷偷转真实模型。

## 真实材料准备与预览

```bash
python scripts/upgrade3/post_body_revision.py prepare --body "<固定BODY.md>" --plan "<最终PLAN.json或json.gz>" --output "outputs/post_body_revision_case/case.json"
python scripts/upgrade3/post_body_revision.py run --case outputs/post_body_revision_case/case.json --variant A --output-root outputs/post_body_revision_trial
python scripts/upgrade3/post_body_revision.py run --case outputs/post_body_revision_case/case.json --variant B --output-root outputs/post_body_revision_trial
python scripts/upgrade3/post_body_revision.py run --case outputs/post_body_revision_case/case.json --variant C --output-root outputs/post_body_revision_trial
```

可用 `--materials` 补入符合协议的现有材料清单。准备入口不覆盖已有输出文件。预览会检查实际审稿/导航输入大小，超限明确失败而不截断。默认配置中 `max_issues=6` 是处理预算，不是要求找六个问题；按 high/medium/low 优先级选择，未选问题保留。输入/材料索引/目标范围上限均可通过 `--config` 显式设置，不是统一学术篇幅模板。

## 真实模型运行仅由本地另行授权

必须同时指定两个付费开关、正的有限额度和共享账本：

```bash
python scripts/upgrade3/post_body_revision.py run --case outputs/post_body_revision_case/case.json --variant A --output-root outputs/post_body_revision_trial --run --allow-paid --budget-cny <已批准总额> --budget-ledger outputs/post_body_revision_trial/shared_budget.sqlite --key-file "<仅本地密钥路径>"
```

B/C 只改 variant，同一轮保持同一账本和总额；这不是每方案各有一份总预算。密钥只供本地客户端使用，不写入 case、配置、消息或发布记录。输入验证/预览不构造 provider。配置拒绝未支持的模型和凭据/目的地覆盖，不静默重试或更换模型。

启用真实调用前，本地须核对提供方当前价格与仓库价格规则是否一致。费用是按已返回 usage 和仓库价格规则计算的费用，不是已核对的账单。缺少 usage 或中断后调用结果不明，费用保持未知并单列已知小计/占额，不能按零元参与比较。账本限制的是显式获准实验范围。

## 恢复与结果解释

每轮保留 manifest、input_case、effective config、baseline、candidate、report，以及 calls/ 内的实际消息、返回和状态。输入、证据、提示词、模型/参数和代码合同参与恢复指纹。已开始但结果未知的调用不自动再付费；请先检查本地账本和已有 raw，再选择新输出目录进行获准重试。并发写同一目录被拒绝，旧锁需要查明进程状态后处理。

`--resume` 只复用完全匹配的合同和已验证的产物。改变材料、问题、参数或提示词应换目录，不能装作同一实验。report 的执行完成、补丁已应用、模型核验判断和公开交付授权彼此分开；没有任何分支自动批准投稿或替换正式 BODY。

## 本地 Agent 盲评

```bash
python scripts/upgrade3/evaluate_post_body_revision.py prepare --case outputs/post_body_revision_case/case.json --runs outputs/post_body_revision_trial/A/live outputs/post_body_revision_trial/B/live outputs/post_body_revision_trial/C/live --output-dir outputs/post_body_revision_trial/evaluation --seed 7 --swap-fraction 0.34
```

准备器验证三个运行使用同一正文和材料，随机左右顺序，隐藏方案名、模型、费用和生成器自评；解码文件单独保存。将 guide、schema 和单个 packet 交给独立的新上下文本地 Agent 阅读实际文本与材料，生成 judgments。不要让写作者给自己打分，不复制 fixture 评价；真实材料大时应按问题读取文件中相关材料，不能只读摘要。

```bash
python scripts/upgrade3/evaluate_post_body_revision.py report --evaluation-dir outputs/post_body_revision_trial/evaluation --judgments outputs/post_body_revision_trial/judgments --output outputs/post_body_revision_trial/EVALUATION_REPORT.json
```

缺失、无效、证据不足或换序分歧均不会生成“质量通过”。重大知识损失、严重新错误、未知费用和录制实验不进入合格前沿。输出是本地 Agent 的有依据判断，不是人工金标准或科学真值；评价费用另记，未知时不捏造。

下一步按 [实验方案](EXPERIMENT_PLAN.md) 与 [本地 Agent 启动说明](LOCAL_AGENT_START.md) 执行。先离线、后小范围真实；没有预定 A/B/C 谁胜出。
