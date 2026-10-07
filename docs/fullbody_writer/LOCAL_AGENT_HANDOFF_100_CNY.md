# 本地执行交接：完整 BODY 写作四路线，新增总预算 100 元

> 已被2026-10-07后续授权更新：本轮所有高级与朴素全文方案合计上限改为40元，含本轮此前费用和未决占额。以下100元数值仅是历史安排，不再执行。请以 [朴素方案及40元开工指令](PLAIN_BASELINES_40_CNY.md) 为当前入口，保留旧账本并显式下调上限。

日期：2026-10-07。实现起点：`80180d9c6fc975e9dfa3430adcfd3a89b361697a`。本文件交接的是**本地真实模型试验**；本轮云端开发只做实现、离线测试与交付，不在云端调用付费模型。

## 1. 先理解这轮究竟要解决什么

用户已有最终批准的完整正文细纲、深细任务与科研材料。本轮不重新检索、不重新精读、不重跑规划，不增加第二个文章 planner，也不重新运行独立首尾模块。目标是把这些上游成果落实为**一位作者持续推进读者理解的完整 BODY**，比较四条有实际差别的写作路线。

所有路线的产品都是同一批准范围的全部 BODY。正文段落由知识关系决定；任务数量不等于段落、块或小标题数量。机制、比较、阴性结果、适用条件与跨章认识都要真正进入成文。不要用“一任务一段”“每章固定几条问题”“每段统一五步法”或任意字数/引用/表格数量限制替代写作判断。充分给模型思考和回答空间，但不要求填满容量。

这是一轮务实的质量改进试验，不要求完整模型×路线的全因子实验，也不要求为了严格对照把旧底稿重新付费跑一遍。先验证机制确实落地，再比较完整成稿的解释价值与总费用。

### 前轮实证，能说明什么，不能说明什么

先读仓库的下列原始记录，不只看本文件摘要：

- `docs/acceptance/writing-candidates-local-20261007/README.md`
- `docs/acceptance/writing-candidates-local-20261007/records/FINAL_REVIEW.md`
- `docs/acceptance/writing-candidates-local-20261007/records/ROOT_REVIEW_A.md`
- `docs/acceptance/writing-candidates-local-20261007/records/ROOT_REVIEW_B.md`
- `docs/acceptance/writing-candidates-local-20261007/records/ROOT_REVIEW_PLUS_REASONING.md`
- `docs/acceptance/writing-candidates-local-20261007/records/ROOT_REVIEW_MAX.md`
- `docs/acceptance/writing-candidates-local-20261007/records/FINAL_BUDGET.json`
- `docs/acceptance/writing-candidates-local-20261007/REPRODUCTION.md`

前轮只真实测试了一章：完整 Ch2，4 个单元、12 项任务、32 份来源。整章 Plus 能展开具体机制；分单元 Plus 有局部解释能力，却丢过整章稿保留的条件。balanced Plus 编辑逐字照抄，增强思考的 Plus 编辑只删除一个引用。Max 自主补回若干关键实验前提与阴性结果，但仍存在重复、材料不支持的强推断，以及主段恢复的限定未贯穿后文和表格的问题。

这说明问题不能只归因于 Plus 不够强、材料没给到或思考上限小，也不能默认再花一次统稿费就有收益。应改变作者的工作对象、真实上下文和改进机制。

**前轮没有生成新的完整 BODY，因此不能评价全文论证、跨章累积理解，也不能据此宣布全文路线胜出。** 前轮 `assembly/BODY.md` 只用于单章装配验证，文件名不能把一章变成全篇。本轮禁止默默把验收缩回一章，或把一章改名为 `FULL_BODY.md` 交差。单章可用于离线接口排错；任何局部真实探针要明确标注、计费，不能代替路线的完整 BODY 结果。

前轮最终新增已结算 3.5213712 元、未确定 7.237796 元，总占用 10.7591672 元；旧 60 元额度剩余 49.2408328 元。它们是**历史记录**，不是本轮重新授权的额度，也不能清零、释放或合并成“多出的免费预算”。本轮新增上限为 100 元。

### 研究依据

本轮设计来自全文路线研究，主要借鉴：LongWriter/WriteHERE 的真实累计稿续写，Re3/DOC 的分层文稿上下文，Open Deep Research 的文章级作者职责，以及 doc-coauthoring 的独立读者检验。没有复制小说事实生成、二次规划、多候选搜索、固定段长、极小输出配额或逐问题无限派生代理。完整研究报告与可追溯链接见本目录 [`RESEARCH_BASIS.md`](RESEARCH_BASIS.md)，不要重新付费搜集同样背景。

## 2. 固定共同输入，保留全部科学含义

### 需要找出的本地原件

1. 最终被接受的全篇 `CURRENT_PLAN.json` 或其实际计划目录，以及完整研究问题、文章主线、范围、目标读者和语言
2. 该完整 BODY 的全部正文章节 ID 与顺序
3. 每章最终批准的编排文件、版本对应的完整材料视图或能够恢复该视图的真实 packet root
4. 现有本地 tokenizer 和既有本地凭据文件；不要把密钥放进配置、命令日志、公开归档或消息

按实际批准状态找原件，尤其核对后续被接受的局部加强是否已经反映在对应章节版本中。不要误拿旧 plan 的章级导航覆盖新任务，也不要混配一个版本的编排与另一个版本的材料。公开 acceptance 投影删去了部分受版权限制的原文，因此不能当成完整实时输入。

使用整篇 manifest，不要求人为为每个文件计算哈希后才能启动。CLI 可离线发现并保存实际输入哈希。示意结构如下；实际 ID、路径和章节列表都必须来自最终批准范围：

```json
{
  "schema_version": "optomind.fullbody_manifest.v1",
  "expected_chapter_ids": ["CH01", "CH02"],
  "chapters": [
    {
      "chapter_id": "CH01",
      "arrangement_path": "path/to/CH01/CHAPTER_ARRANGEMENT.json",
      "view_path": "path/to/CH01/ARRANGEMENT_INPUT.json",
      "packet_root": "path/to/accepted/packets"
    },
    {
      "chapter_id": "CH02",
      "arrangement_path": "path/to/CH02/CHAPTER_ARRANGEMENT.json",
      "view_path": "path/to/CH02/ARRANGEMENT_INPUT.json",
      "packet_root": "path/to/accepted/packets"
    }
  ]
}
```

上面两章只是 schema 示意，**不得当作实际测试范围**。输入 `--plan` 时从真实最终计划读取全篇原始目标与章序；没有可解析计划时，`expected_chapter_ids` 必须明确列出全部 BODY，而不是用当前 manifest 恰好包含的几章反过来定义“全部”。计划/目录指针按其所在目录解析相对路径。

模型实际需要看到：

- 原问题、文章级主线与不可改写的完整原细纲
- 当前写作范围的完整任务、来源用途、条件、比较与承接职责
- 这些任务对应的完整可用科研材料、互补材料和稳定来源身份
- 所属路线要求的真实已写正文，而不是只有“前一章应该负责什么”

科学材料完整不意味着强制每项来源拥有自有 A/B 或全文。可用的综述转述与明确引用身份正常参与；不能因为没有直接精读就自动踢掉来源。精确重复可以去重，互补信息、阴性条件、不同证据层次不能以“节约上下文”为由裁掉。若详细来源材料与规划概括冲突，来源支持程度优先；不得要求模型把错误任务推断写成事实来完成清单。

## 3. 先验收实现，未过关不要付费

从仓库根运行 CLI `--help`、有关 offline tests 和正式 preview。以当前源码实际输出为准，不用临时写作脚本绕过正式实现。

```bash
python scripts/upgrade3/fullbody_writer.py --help
python -m pytest tests/upgrade3/test_fullbody_contracts.py tests/upgrade3/test_fullbody_engine.py tests/upgrade3/test_fullbody_cli.py tests/upgrade3/test_fullbody_integration.py
```

所有需要的依赖和 tokenizer 优先复用本地已安装版本；不能把下载新模型算作隐含准备步骤。

离线准备与预览示例：

```bash
python scripts/upgrade3/fullbody_writer.py \
  --manifest path/to/BODY_MANIFEST.json \
  --plan path/to/accepted/CURRENT_PLAN.json \
  --prepare-manifest outputs/fullbody_local_100cny/SOURCE_INPUTS.json

python scripts/upgrade3/fullbody_writer.py \
  --manifest outputs/fullbody_local_100cny/SOURCE_INPUTS.json \
  --route whole_author \
  --config config/fullbody_writer/plus_first.json \
  --tokenizer path/to/local/tokenizer.json \
  --output outputs/fullbody_local_100cny/whole_author
```

后续预览和所有真实路线都消费准备得到的 `SOURCE_INPUTS.json`，不重新追逐可能改变的 `CURRENT_PLAN` 指针。准备后的依赖哈希由程序检查，无需人工逐项填哈希；同一输出目录如果来源版本变化，应明确停止并核对，不能混用。

不加 `--run` 为离线预览；`--responses` 是录制回放，二者都不是新的真实科学写作结果。预览可以估容量，不能假装知道实际模型词数、下一轮真实稿件或实际账单。缺本地 tokenizer 的保守估计可能误判超限，先检查估算依据，不要因此删材料。

在任何真实调用前确认：

1. **全文边界真实。** 批准章序、全部任务与 manifest 一致，没有只载入一个章节却自称 complete 的路径；空正文、空任务、缺章和重复章能被发现
2. **供材实际送达。** 检查 actual messages/input projection，不只检查磁盘上是否存在材料。全文材料在 common pool；容量窗口内对应任务的条件和相关材料完整
3. **原计划不变。** 所有路线都保留完整原细纲与文章目标；导航、写作位置和临时摘要不得改写或替换它
4. **连续路线有真实前文。** 后续请求包含实际已接受正文原文，内容与前次输出一致；不能只多传一个全文标题或邻章职责便宣称连续作者
5. **工作台可回读。** 最近正文为原文，远处索引绑定真实版本；请求有效 segment ID 能取回完整原文，`read_source_handles` 可从完整来源身份导航取回当前未驻留的批准材料及必要来源归属链。远处正文和来源只是未驻留，不是被永久截掉。回读后额外作者调用照实计费
6. **独立读者不被喂答案。** 只看到完整 BODY 和用户问题/读者目标，不见人工科学问题清单、旧 Max 优秀答案、作者自评或预设问题配额
7. **修订有证据。** 编辑器从问题定位恢复相关完整任务与来源材料，并收到真实原稿。独立读者不知道内部 task ID 也必须可运行；模糊/重复/不存在 anchor 不能误改正文
8. **职责范围准确。** 全文读者、完整稿编辑与局部问题窗口有不同范围说明；不要继承旧章级编辑里“未读全章”的矛盾句
9. **未完成状态诚实。** 输出截断、只写部分任务、未闭合响应或只有目录都不能升级成完整 BODY；已写正文与 raw response 要保留，供恢复使用
10. **预算与恢复可靠。** 所有 live 调用经过同一账本；包括 reader、reviser、继续生成、回读后再调用和失败重试。成功阶段可复用，配置/输入/提示词变更不能误命中旧缓存

离线测试证明合同和程序行为，不证明实际科学质量。若实现有可修复接缝，先在授权开发范围修复并重测；不要花钱用模型掩盖实现错误。不得引入题目特定答案、改变上游科学内容或追加一套 planner 来“修好”本轮。

## 4. 本轮预算与模型选择

### 一个新实验账本，所有路线共享 100 元

最简单且不会改写旧账的方式：在本轮目录建立**一个**新增实验账本，绝对总上限 100 CNY。所有四条路线、模型分支、reader/reviser、续写、恢复与失败尝试都传同一个路径。旧试验账本保持原样。

`--budget-limit 100` 指所选账本的绝对累计上限，不是“再加 100”。不能在每条路线建立新的 100 元账本，也不能覆写旧 ledger 的 cap 伪装成合法增额。若坚持复用历史账本，需要明确、可审计的增额流程和本轮 reservation-ID 起始快照；CLI 不会偷偷替你完成这个操作。不要同时启动未登记到本账本的其他模型脚本。

实验结束须分开报告：已结算费用、仍预留费用、未确定费用和剩余额度。总占用 = 已结算 + 仍预留 + 未确定；未知送达或未知用量不等于免费。即使失败稿、废弃路线或中止尝试没有进入最终 BODY，费用仍在本轮总额中。保留原始 usage、实际请求模型和参数；reasoning 若已包含在 completion 中，不能重复加算。实际 provider 缓存命中按记录报告，不假设重复输入免费。

### Plus 优先，Max 最后，Flash 只在有具体价值时分支

- `config/fullbody_writer/plus_first.json` 为默认。作者、读者和修订都先用 Plus；给出充足的思考与回答容量
- `plus_reasoning.json` 是有具体理由时可试的 Plus 思考/回答容量取舍，不能因为文件存在便四路线全部重跑。上限提升不等于实际更充分思考，更不保证质量改善
- `economy_flash.json` 只作为可解释的经济性分支；不能为了花在预算内而未经说明把主试验降级成 Flash、缩短后半篇或删除材料
- `selective_max.json` 只把修订角色升为 Max，须明确选择该配置并传 `--allow-max`。先用已完成底稿及 Plus 路线识别重要困难，再决定 Max 是否值得；没有自动升级，也没有默认全篇 all-Max 重跑

各角色实际 `model`、`thinking_budget`、`max_output_tokens`、流式超时、输入估算与最终 wire 参数必须存档。当前配置使用 900 秒无活动期限和 3,600 秒整体期限；这两个参数不能再次互相覆盖。容量值是上限，不是最少输出或科学质量指标。

本轮不预先为路线设置逼迫少写的细碎小额度。每次发起前检查剩余资金能否覆盖安全预留；如果不足，先保留结果并报告，还差哪些完整工作。不要释放 uncertain、悄悄降低科学范围或超支以换取“全部完成”。

## 5. 四条认真执行的完整 BODY 路线

使用同一份已锁定输入和独立输出目录。先跑能尽快得到完整底稿的路线；对四条路线都完成机制核对和适用性判断。适用且预算允许的路线应生成真实完整 BODY，不以 preview 或一章代替结果。容量确实不允许的路线应留下可复核的不适用/未完成记录，转向能够完整完成的路线，不强迫小稿来填比较表。

### A. `whole_author`：全文受托作者

输入与输出容量允许时，先用 Plus 直接完成全部 BODY。一个作者拿到文章级问题、完整原细纲与相关完整材料，按读者理解推进成稿。默认不接隐藏统稿器。

先检查它是否真的写完，尤其看后部是否以摘要、计划句或空标题敷衍。若单次输出不足，保留 raw 与部分稿及费用；不能通过压短 BODY 来保住“一次写完”的名义。若 route 明确因输入容量拒绝，记录拒绝发生在送达前还是送达后。真实 full-body 失败同样有价值，不把它算成零费成功。

### B. `continuous_author`：读真实前文的连续作者

在同一完整 BODY 上继续推进；后续调用带已接受正文原文和当前完整任务/材料。实现默认以批准章界作为有意的质量窗口，即使整篇输入能容纳也可分次；容量不足时按完整任务边界调整。它仍是读真实前文的同一作者，不是各章独立写完再拼接。质量性分段和供应商容量限制是不同理由，日志如实区分。

完整原细纲始终可见，实际前文决定承接；不默认额外全篇统稿。最后完成全部授权任务才有完整结果。比较时重点读跨章接缝、前文认识是否真正成为后文起点，以及后文引用同一研究是否有新用途。

### C. `workbench`：可完整回读的长文工作台

在相同完整任务上试分层文稿上下文：原纲要和全文定位固定，当前任务与来源完整，最近实际正文驻留，远处正文有完整可恢复的索引与版本。作者可以按真实需要读取远处段落；不要人工注入应回读的科学答案。

要核对远处原文确实可完整取回，而不是只能取一个先验摘要；还要核对 `source_navigation`/别名导航与 `read_source_handles` 能取回完整批准材料，不靠前文成稿代替事实依据。为检验回读机制可做离线录制测试；真实 run 是否调用回读由文章需要决定，不为凑次数强迫回读。不需要回读的成稿可以保留，但必须说明那次 live run 没检验“模型会自主选择远处回读”这一能力。

如果原输入足够小，workbench 与 continuous 实際没有不同驻留行为，就准确记录差异未触发，不能据此声称已证明工作台节省了长上下文成本。若窗口需要把密切比较的来源拆开，先修正窗口与供材设计，不裁掉条件让调用通过。

### D. `reader_revision`：复用完整底稿，独立读者驱动修订

复用 A/B/C 中**首份确实完整、出处可追溯的 live BODY**，并明确指出来自哪个结果文件及哈希。正式付费 D 不能接受离线 recording/custom 结果冒充 live 底稿；录制底稿仍可用于预览与回放。不为测试编辑器重新生成底稿。先独立读完整稿与原始问题/目标，让模型说明实际理解并定位值得修的问题；它没有人工科学清单或每章问题配额。

编辑器随后获得真实原稿、问题锚点及相关完整任务/材料，判断问题成立与否，按精确唯一锚点修订。可以驳回错误读者意见；材料不支持的“改进”不能照做。未涉及的有用内容保持，必要条件应在所有受影响的后文概括和表格中保持一致。没有值得修改的问题就保留原稿，并把这次阅读及判断完整归档。

默认先做一轮真实全文阅读与必要修订，依据收益决定是否有必要继续；不能把所有角色反复调用一遍当作自动质量保障。若又增加模型核验或二次 reader，须在本轮账本中明确记录，不藏在“评测免费”里。人工后验审读可以评价实际效果，但不能给候选作者喂一份本题科学正确答案清单。

费用要有两个口径：本轮新增调用的实际总费用；完整 D 方案的底稿成本加 reader/reviser 增量。底稿从同一 run 复用时不向账本二次扣费，但也不在方案比较中隐去底稿成本。

## 6. 正式本地运行示例

用本地已验证的 Python/环境替换 `python`；以下路径是占位，先替换为真实原件。shell 换行语法可调整，参数含义不要改变。不得把凭据文件内容粘进参数。

```bash
python scripts/upgrade3/fullbody_writer.py \
  --manifest outputs/fullbody_local_100cny/SOURCE_INPUTS.json \
  --route whole_author \
  --config config/fullbody_writer/plus_first.json \
  --tokenizer path/to/local/tokenizer.json \
  --output outputs/fullbody_local_100cny/whole_author \
  --run \
  --budget-ledger outputs/fullbody_local_100cny/budget.sqlite \
  --budget-limit 100 \
  --key-file path/to/existing/local/key_file
```

B/C 使用相同输入、配置、tokenizer 和**同一个 budget.sqlite**，将 `--route` 分别设为 `continuous_author`、`workbench`，输出目录分别命名。省略 `--budget-limit` 可沿用已建立账本的有限上限；保留同样的 100 也不能把它当成新赠送额度。

首份完整稿可用于 D：

```bash
python scripts/upgrade3/fullbody_writer.py \
  --manifest outputs/fullbody_local_100cny/SOURCE_INPUTS.json \
  --route reader_revision \
  --draft path/to/first_complete_run/FULL_BODY_RESULT.json \
  --config config/fullbody_writer/plus_first.json \
  --tokenizer path/to/local/tokenizer.json \
  --output outputs/fullbody_local_100cny/reader_revision \
  --run \
  --budget-ledger outputs/fullbody_local_100cny/budget.sqlite \
  --key-file path/to/existing/local/key_file
```

Max 分支只在确认它有具体价值后使用 `selective_max.json` 加 `--allow-max`，保持同一科学输入与可复用的成功阶段。先查看该分支实际复用了什么，不能仅因传了 `--draft` 就假设读者或其他阶段也一定命中缓存。

## 7. 恢复：先保住已付费成功结果

- 保留 output、raw response、actual request、usage、attempt 与 ledger。重新运行相同正式命令应复用成功阶段，不重付整篇费用
- 未完成/失败阶段默认不要盲目重复。检查是否有已送达、已返回但仅解析失败的完整内容，是否能在不改科学正文的前提下离线恢复
- `--retry-failed` 是明确重试失败阶段的选择，不是“免费恢复”；新调用仍要预留和计费。先确认没有另一个仍在运行的相同调用
- `--continue-incomplete` 用于 `continuous_author`/`workbench` 中有明确 `finish_reason=length` 和可用真实正文前缀的续写。作者收到 `current_scope_partial_body_markdown`，只返回精确尾文，程序不插分隔符直接拼接；整个当前范围仍须完成后才能接受。它与 `--retry-failed` 不得同时使用，也不能把 uncertain/超时当作已知输出上限。whole_author 不能靠此选项暗中变为多次调用路线；需要改路线时明确另记
- 只在有完整返回依据时恢复为完成。SSE 部分流、缺 finish/usage 或进程消失不能证明免费，也不能证明模型已完整写完
- 输入、配置或 prompt 改变后查实际缓存指纹与复用清单。保存实际版本关系，禁止把录制回放结果冒充新增 live 成稿
- 格式恢复必须能够证明科学正文未被人手编辑。若人为修正文稿，另存人工稿，不能把它当自主路线原始产物
- 更换本地输出目录、版本或模型时，仍用本轮同一个账本。废弃结果和失败成本不消失

显式安全控制如 `max_author_calls`、`max_rereads_per_window` 与最近驻留 segment 数属于运行保护/上下文配置，不是文稿字数或科学问题配额。若它们阻碍一个有价值的完整任务，先保留当前结果，评估实际剩余预算后显式调整并记录，不把达到次数上限等同于 BODY 已完成。

## 8. 再验收完整结果，不靠统计替代亲读

先做结构核查：实际全篇章序与授权范围一致，正文不是空占位；模型任务声明与真实内容相符；没有截断、意外复制前文、丢失尾部或引用身份错接。只列出全部任务 ID 并不等于任务已完成，`complete:true` 也不是质量证明。

随后完整读实际 BODY。若由不同审读者分担，可以先各自读，但最终仍须有人掌握同一篇全文的论证，不能只读每章开头或各抽一段便说“全文亲审”。重点回答：

- 读者是否逐步获得对原问题有用、可解释的认识，而不只是逐项完成任务书
- 重要机制和比较关系是否真正展开；后半篇是否同样有足够深度
- 条件、反例、阴性证据与不一致发现是否改变了判断，而不是被隐藏或模板化免责声明冲淡
- 前文认识能否成为后文起点；跨章重现研究时有没有新的解释用途
- 原纲要中的可疑推断是否按具体来源支持程度落实，而不是无条件照写
- 来源身份、直接/转述边界、观测与因果层次、正文与表格是否一致
- 语言是否平实清楚、开头有目的且自然多样，是否仍出现任务命令、套路式宏观开篇或机械收尾
- D 的实际改动是否帮助读者理解，是否保住原稿有用内容，有没有引入新问题
- 同样的最终完整目标下，各路线的质量收益、实际成本、恢复可靠性与适用边界是什么

这些是判断维度，不是要逐条写进每段的模板，也不是强制均匀抽样或打分表。引用数、字数、表格数、改动字符数可以作诊断信息，不能作为成熟度的替代指标。具体比较要指出真实文本与来源支持，允许暂时没有赢家。

不要把人工发现的问题反向注入下一候选成为题目专属提示词。需要验证产品改进时，修改一般性的上下文/职责/供材机制，保留自主识别能力，并明确指出配置差异和重试原因。

## 9. 必须交付的结果与停止条件

本地交付应包括：

1. 实际源码版本、已运行的离线检查、共同完整输入与自动发现的版本哈希
2. 四路线各自的实现验收与执行结果：完整 live 成稿、明确容量不适用、或仍未完成及原因；不能混称成功
3. 每份实际完整 BODY 和机器可读结果；D 的底稿、独立读者结果、修订前后稿及实际差异；选中最终稿的明确来源和哈希
4. 完整 raw/provider usage/actual parameters/attempt/resume 关系，以及本轮共同预算最终对账，包括废弃和 uncertain
5. 基于完整阅读的质量判断、重要未解问题、建议继续采用哪条路线与何时切换，不假称已经达到投稿水平或跨领域实证
6. 可复核的公开归档投影；保留来源身份和必要证据，但不上传密钥、认证信息、受限论文全文或整个私有账本。公开投影和私有精确请求必须明确区分

正式文件定位：CLI 在输出目录记录 `SOURCE_MANIFEST.json`、`CLI_CONTEXT.json`、`CLI_RUN.json` 和 `cli_invocations/`；引擎在 `runs/<run_id>/` 保存本次 `FULL_BODY_RESULT.json`、`FULL_BODY.md` 和 `IMPLEMENTATION_REPORT.json`，顶层保存当前选用版本。D 另保留 `ORIGINAL_FULL_BODY.md`/`ORIGINAL_FULL_BODY_RESULT.json`。报告要检查 `current_run_version`、`selected_version` 与 `selected_input_matches_current`，不能把先前完整顶层稿误认成本次失败运行的新成果。

不要求自动推送、发布或改变生产默认路线。本轮交接没有 push 授权；完成后先报告验证过的结果。

在全部适用路线得到完整结果并完成比较、预算无法继续覆盖所需调用、输入原件缺失且无法可靠恢复、或必须取得新授权才能继续时，停止依赖步骤并给出明确结果/阻碍。预算够且工作仍有可安全恢复的失败时，不要停在中间状态；优先恢复已成功阶段并完成整篇。绝不通过缩水为单章、隐藏失败费用或人工喂科学答案来制造“完成”。

### 条件触发的补材修订费用

编辑器若提出超出初次供材范围的相关位置修改，程序可能保存该提案，取回相关完整材料后再调用一次修订角色。阶段名会包含 `evidence`，报告 `evidence_supplement_required` 及实际供材范围；这些调用必须计入100元共同账本。不是所有问题都要补材，也不允许把已有可用的综述转述判成“必须重新找原研究”。检查最终日志时将初次编辑和补材后的编辑分别列出，避免误报“只调用一次”。

最终 `FULL_BODY_RESULT.json` 已包含全文输入位置、批准章序和来源身份文件指针。传给后续消费者时连同这些关联文件保留，不要只搬走 Markdown 而丢掉版本与来源。

### 局部读者问题的解释

请分别查看 `body_complete`、`reader_revision_complete` 和 `pending_reader_issues`。一个读者位置未能精确定位，不等于已有全文不完整，也不应抹掉已成功的改进；程序保留可执行的问题和有效补丁，剩余问题明示，不自动重复阅读全篇。由你结合实际影响决定是否值得继续处理，不能为了清空轻微警告消耗整轮预算。
