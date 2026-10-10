# OptoMind 运行须知

本文件记录已实际踩过的参数与数据交接问题。每次运行先核对当前源码、实际入口和最终发送消息，避免“配置看似正确，模型收到另一份输入”。已结合2026-10-06提供的新版67,286行本地对话记录更新；原39,518行记录不再作为最新背景。事件定位和当前落实状态见 [记录对照](RUN_GUIDE_EVIDENCE.md)。

## 正式操作入口（2026-10-10 收尾）

全链路输入输出统一见 [PIPELINE](PIPELINE.md)。本节是当前操作说明；后文保留旧事故、实验和预算教训，日期较早的“当前候选/本轮预算”不覆盖本节。

正式版本是 **baseline**：Plus分单元写作 → 任务核查及必要的一次有界补写 → 装配 → 一次全文局部编辑 → 实际选用稿编号。`chapter_coherence` 是默认关闭的实验；指南作者、全文候选不进入默认流程。已认可的完整稿为7章29单元、187个规范化论文身份；它是此前生成、选择及免费恢复的结果，本次没有重新生成正文。

### 准备与免费预览

以下命令从仓库根执行；大写文件名/`<…>`替换为本轮实际路径和数值。`FULL_BODY_INPUT.json`与manifest二选一。manifest必须绑定完整已批准章包和对应有效编排，而非单组加强结果；格式见 [输入合同](../writing_candidates/LEGACY_UNIT_QUALITY.md)。

```powershell
# 可选：固定既有manifest版本及输入哈希；不启动全文候选作者。
python -X utf8 scripts/upgrade3/fullbody_writer.py --manifest BODY_MANIFEST.json --prepare-manifest PREPARED_BODY_MANIFEST.json

# 默认免费，导出全部实际请求；不读密钥、不创建付费账本。
python -X utf8 run_review_harness.py --delivery-start body --delivery-input FULL_BODY_INPUT.json --delivery-out outputs/my_delivery --tokenizer TOKENIZER.json

# 另一正式输入路径，使用同一manifest loader。
python -X utf8 run_review_harness.py --delivery-start body --delivery-manifest PREPARED_BODY_MANIFEST.json --delivery-out outputs/my_delivery --tokenizer TOKENIZER.json
```

预览不能代替生成；检查 `<delivery-out>/body/FULL_BODY_INPUT.json`、`RUN_REPORT.json`（status=preview）及各单元 `UNIT_INPUT.json`/`UNIT_MESSAGES.json`中的任务、用途、条件和完整材料。manifest模式还保存 `SOURCE_MANIFEST.json`。主作者使用章节框架和其他单元职责，不带实际已写前文；不能把指南连续写作的前文合同套到此处。

### 正式运行和同目录恢复

```powershell
python -X utf8 run_review_harness.py --delivery-start body --delivery-input FULL_BODY_INPUT.json --delivery-out outputs/my_delivery --tokenizer TOKENIZER.json --run --ledger PROJECT_BUDGET.sqlite --budget-limit <absolute_CNY_cap> --budget-scope <persistent_scope> --key-file LOCAL_KEY_FILE.txt --key-index 1
```

密钥只通过既有本地配置读取，不复制进输出或文档。可在同一命令上加 `--account-ledger ACCOUNT_BUDGET.sqlite --account-budget-limit <account_lifetime_cap> --account-mapping-dir ACCOUNT_RECEIPTS_DIR`，作为跨项目账户总账；与项目账本重复的物理调用不能相加算两次费用。

恢复时重复**同一**命令、输入、目录、模型参数、项目/账户账本、scope及累计封顶。兼容成功响应和质量/编辑结果复用，不重买成功单元。无 `--run` 的同目录执行只能预览或免费重放已有响应；缺失步骤不会自行联网。

```powershell
# 保存质量RAW重新解析：不调用模型。正式入口缺省quality_control=True。
python -X utf8 run_review_harness.py --delivery-start body --delivery-input FULL_BODY_INPUT.json --delivery-out outputs/my_delivery --tokenizer TOKENIZER.json --reparse-saved

# 只有确认需付费重试时，在原正式运行命令上添加：
# --retry-failed
# 保留原尝试/占额；仅合格的无可回放输出失败允许新尝试，不自动反复重抽。

# 显式关闭质量步骤；不会把残余诊断判成“科学问题已解决”。
python -X utf8 run_review_harness.py --delivery-start body --delivery-input FULL_BODY_INPUT.json --delivery-out outputs/author_only --no-quality-control --no-article-edit

# 显式实验，必须独立目录；回退用baseline和原baseline目录。
python -X utf8 run_review_harness.py --delivery-start body --delivery-input FULL_BODY_INPUT.json --delivery-out outputs/coherence_experiment --body-version chapter_coherence
```

`--reparse-saved`只处理质量RAW，不等于任意writer截断响应都能恢复。新补写/核查/编辑仍需原账本下的 `--run`。`--only-unit CHAPTER:UNIT`为局部测试，保留全文输入语境，但不生成完整稿或运行全篇编辑。

### 默认参数与配置优先级

| 入口/角色 | 默认值 | 有效覆盖方式 |
|---|---|---|
| 正式harness BODY | baseline，quality_control=True，article_edit=True | `BODY_DELIVERY_DEFAULTS`集中设定；明确CLI开关覆盖 |
| 历史独立 `legacy_unit_writer.py` | baseline，两质量开关False | 用 `--quality-control --article-edit`才等同正式质量步骤 |
| 作者/补写 | qwen3.5-plus，8192思考＋32768回答 | 作者CLI `--thinking-budget/--output-tokens`；质量阶段容量按其运行库角色profile，不随作者值一起变 |
| 核查/全文局部编辑 | qwen3.5-plus，16384思考＋24576回答 | `unit_realization.py`、`legacy_unit_route.py`角色profile |
| downstream首尾 | 不构造隐式live模型 | config明确fixture/严格recordings；live使用受预算约束的provider注入 |

`max_output_tokens`是回答空间，实际思考请求的总 `max_completion_tokens`为思考＋回答。正式BODY不是把 `quality.json` 中其他实验的Flash writer默认搬进来；也不需要研究路径的premium开关来启用Plus。最终核对实际 `profile/effective_request`，不用配置名字代替wire参数。预算CLI缺省值是30，但接手已有账本时必须显式传其原累计封顶；它不是本轮新增额度。

### 选用稿、状态和退出码

- 输出根是 `<delivery-out>/body`。`RUN_REPORT.json`保留作者/质量/编辑全过程；`DELIVERY_REPORT.json.selected_body`是唯一选用声明，含 `source`、`handles_draft`、`reader_draft`、`references_path` 和编号报告。
- 有效全文编辑后 `source=article_edit`；编辑关闭时用装配稿。首尾读所选**句柄稿**，BODY读者稿/目录直接可用；有首尾和图注新增后，04对最终全文重新编号，05只消费04产物。
- `body_delivery.ready=true`、`status=complete_with_diagnostics`表示正文完整可交付且附诊断。原 `pending_problems`、`problems_resolved=False` 和 `scientific_acceptance=False`继续保留；不再由这些已终结诊断误挡BODY消费者。
- 未写全、选中子集、装配失败、所选文件缺失、编号未完成、要求的核查或全文编辑没有完成，`body_delivery.ready=false`并记录 `blocking_reasons`。history/plan受限导入与缺录制仍用原严格装配门槛。
- 两个BODY CLI：免费preview为0；完整BODY（含诊断）为0；生成/所需阶段不完整为2；显式下游配置未完成也为2。非法参数/配置可能由argparse或异常返回非零；按报告定位，不能把所有非零都当模型内容失败。

### 显式首尾与出版

不传 `--delivery-config` 时，BODY交付选用正文及编号，不自动生成首尾/PDF。下例为离线fixture/严格录制接线，文件路径相对于config；fixture协议见 [首尾说明](../POST_BODY_MANUSCRIPT_PARTS.md)。

```json
{
  "schema": "review_v2_delivery.config.v1",
  "research_question": "本篇原研究问题",
  "language": "zh",
  "front_back": {"mode": "post_body", "fixture": "parts_fixture.json"},
  "post_body_context": {"path": "context.json"},
  "figure_assets": [],
  "compile_pdf": false
}
```

```powershell
# 首次显式接入下游；已有正文缓存可复用，03首尾目录必须尚未生成。
python -X utf8 run_review_harness.py --delivery-start body --delivery-input FULL_BODY_INPUT.json --delivery-out outputs/my_delivery --tokenizer TOKENIZER.json --run --ledger PROJECT_BUDGET.sqlite --budget-limit <absolute_CNY_cap> --budget-scope <persistent_scope> --key-file LOCAL_KEY_FILE.txt --key-index 1 --delivery-config DELIVERY_CONFIG.json

# 独立消费已选句柄稿，不重跑BODY；首尾必须使用新输出目录。
python -X utf8 scripts/upgrade3/manuscript_parts.py --draft SELECTED_HANDLES.md --research-question "原研究问题" --context context.json --fixture parts_fixture.json --output outputs/parts_run01
```

BODY的02阶段记录 `body_selected`，不会再全文编辑一次；03读取其实际路径，04读取03返回的final稿，05读取04 reader/REFERENCES/图映射/assets。BODY模式未显式指定 `identity_catalogs` 时使用本次selected_body的 REFERENCES；显式目录优先（显式空列表也不会偷偷补目录），扩增背景文献需明确材料身份。不能拿另一个run的裸P号目录顶替。

03的owned首尾块保持原样；BODY+post_body到04的投影只移除旧的未受管理书目，防止引用列表替换吞掉结语标记。03原稿及投影哈希单独保存。`compile_pdf=true`才请求TeX/PDF，本机需Pandoc/TeX与可用字体；Markdown出版不需要LLM。出版默认关闭Crossref/S2 enrich。

**恢复边界：**BODY同目录支持缓存恢复；现有 `post_body` 首尾仍要求新输出目录，同一config重复进入非空03会明确停止。恢复BODY时先不传config；需要再次生成首尾时独立CLI用新目录及当前selected句柄稿，或由受控provider驱动显式运行。不能宣称整个02–05都已有同目录恢复。CLI/config目前没有live首尾适配器；fixture接通证明接口，不是新的真实首尾质量验收。

### 费用与本次验证范围

账本按累计已结算实际费用＋reserved/uncertain约束原封顶；超时不能当免费，换目录/恢复不重新起算预算。本次只离线控制、实际请求预览与原29单元免费恢复，新增API费用0。原60元项目账本结算17.5659408、占额0、剩余42.4340592元；这个余额是状态记录，不自动授权新实验。已有29单元及187身份来自此前认可稿，详见 [历史对照归档](../acceptance/outline-to-body-coherence-20261010/README.md)。本次验收命令与结果追加到 [RUN_GUIDE_EVIDENCE](RUN_GUIDE_EVIDENCE.md)。

## 产品目标

质量与解决问题优先，严格拒绝过度防御。不要靠缩减有效材料、压低思考额度或隐藏失败让程序表面运行成功。修通用链路，不向生产请求注入本题人工科学修法。综述转述的原始研究，只要内容可用且身份明确，可以正常、同权参与。

目标是能建立知识、解释背景概念、展开机制与比较的完整综述 BODY，不能退化为 Facet 问答拼接。具体研究问题只负责确定任务，不成为写死领域答案的模板。

### 把拒绝过度防御落实到操作

- 发现确定的材料损失、错误参数或消费者接线就修，验证生效和回归；不为已经决定处理的瓶颈另开无休止归因实验。
- 允许按本轮验收口径接受的局部模型误差。少量措辞或研究归属问题先保留记录，不能默认升级成整条链停机或追加付费审稿；身份缓存串号、读错版本、正文被覆盖等确定工程故障另行定位处理。
- 有用但不完整的答案按 partial 保留，缺口没补齐不等于整篇资料没价值；小格式差异能离线解析恢复时，不让已付费成果整份报废。
- 编排和写作可以根据真实材料进行解释、补充遗漏条件或修正明确的局部措辞，不逐句回交审批。改变核心判断、章节分类依据或跨章职责时才回到相应负责人。
- 一张充分的综合表可以完成多个任务；按实际内容验收，不按表格数量追加工作。合理 no_change 和纯换行变化都不计为科学改进。
- 获授权的阶段完成后及时读取产物并决定下一步，不在已到停点后空等。需告知风险时说清具体影响与解决办法，不以免责式表述代替行动。

### 持续工作准则（2026-10-06 用户补充）

本运行须知也是后续开发、运行与评价的执行备忘录，每次相关工作遵循这里的要求。

- 字符上限、输入容量、思考额度和输出空间应按任务需要设置得充分宽裕，不以保守的小上限作为省钱默认策略，避免材料经常被截断。
- 核对完整原材料与实际发送的消息，尤其是末尾条件、限制和补充答案。文件已保存、路径已传入或 packet 已包含材料，都不能代替确认模型实际读到的内容。先消除程序造成的半成品输入，再判断模型表现。
- 超出模型实际容量时按完整任务或完整材料分批，保留必要的共同语境；不靠静默截尾维持调用。上限是可用容量，不要求模型凑满输出。
- 我们做的是解决实际问题的产品。质量与实际收益优先，不为追求严格公平对照、精确归因或控制所有变量额外消耗时间和预算。
- 调研、成本评价和方案取舍，只要现有产物、粗略估算与小范围验证足以支持下一步，就直接推进。只有比较会实质改变选型或避免明显浪费时，才补必要的对照。
- 测试优先验证材料完整送达、参数实际生效、功能可用和重要回归。接受合理模型误差，不把零误差或研究级证明设为产品改进的前提。

## 运行前固定什么

记录完整源码 SHA、启动命令、输入文件与哈希、模型/预算 profile、实际阶段顺序、输出目录和选定恢复来源。所有模型调用使用可追溯的 call_id。新实验用新目录，不覆盖历史产物。

运行中修改磁盘源码不会更新已启动进程：结果必须绑定启动时实际加载的版本。恢复前先确认相关进程是否仍在运行，避免重复启动同一付费任务。

每个消费者应绑定本轮明确选定的上游产物路径与 SHA。历史上第二次编排已经修好，writer却误读第一次编排；不要按目录更新时间、默认路径或“第一个文件”猜哪份该用。选择可以由运行器自动记录，不要求生产每一步都有人签字。

当前正文顺序继续保持：全局协调及负责人修订 → 正式案例附加 → 编排 → 写作 → 装配。函数名 `_post_case_review` 不能作为调整执行顺序的依据。独立细纲加强属于显式可选路径，首尾模块不属于 BODY 实验。

## 思考与输出预算

- 思考额度与回答空间是两项参数，不得在中间适配器里被旧默认值覆盖。
- profile 中的 `max_output_tokens` 表示回答容量；思考开启时传输层按对应模型接口发送总 `max_completion_tokens`。例如思考 32,768＋回答 32,768，总量为 65,536。
- 不同时盲传旧 `max_tokens` 与新参数；最终以保存的 `effective_request`、模型返回名、usage 和 finish_reason 为准。
- 不以“强/标准/便宜”等档名代替实际模型名；历史驱动中的局部默认值也要清理。Qwen3.8 不应同时发送 `reasoning_effort` 和 `thinking_budget`；当前 `mapped_reasoning_effort` 是遥测记录，不是另一个 wire 参数。
- `finish_reason=stop` 不表示思考额度一定够用；检查 reasoning_tokens 与配置上限，以及 cap_pressure。
- 不把大上限当作模型必须凑满的字数，也不把较短输出直接当作质量差。
- 区分正常内容写到上限与循环重复退化。若响应反复复制同一来源或任务，不能只不断加大输出额度重试；先核对 system/user 是否要求了互相矛盾的工作，保留已付费原返回。
- 编排、writer、补写与辅助判读都有独立默认值。切换入口或模型后重新核对，特别防止“BODY 开启思考、局部实验却关闭”。

当前主要 profile 与默认容量见 `config/outline_revision/quality.json` 和 `QUALITY_CAPACITY_HANDOFF.md`。例如细纲 Plus/Max 局部增强均为思考 32,768＋回答 32,768；编排为 16,384＋32,768；writer/补写为 8,192＋32,768。CLI、注入客户端和配置覆盖后仍须检查实际请求。

整章增强才使用 `_chapter` 的32,768＋65,536配置；诊断/评价当前为16,384＋24,576。历史卡片实验曾用512思考，当前卡片默认已是4096，不能把历史成功参数直接恢复成当前默认。

## A/B 与材料可见性

### 正式规划入口

- `ProgressivePlannerConfig.planning_revision_enabled` 与 CLI 默认开启。
- `--planning-revision` 仍兼容；显式 `--no-planning-revision` 仅用于确有必要的历史编排模式，不再允许以短路由替代完整 A/B。
- 路由说明只说明用途，不能充当论文的科研材料。章节细化请求应同时有 A、B 和独立 routing_note。
- 保存了完整 writer packet，不等于上游负责人读过完整材料；检查实际模型输入。

### 材料获取与卡片导出

- 下载成功、HTTP 200、解析出文本不等于取得论文正文。检查论文身份和方法/结果等实质内容，注意反爬页、摘要加BibTeX、会议相邻论文混入、表格条件或图注丢失。
- 普通整篇阅读保留实际取得的完整科学内容，不按 Facet 先裁正文；定向局部补读则保留其问题与阅读范围。材料获取范围和阅读用途要区分。
- 降本优先去掉完全重复的包装和重复摘要，不能模糊去重掉科学正文、互补精读、不同任务条件。卡片一句话总结也不能替代实质阅读。
- 导出 B 时同时保留规划用途、facet贡献、广泛用途和范围限制；当前联合提炼的 A 也可能受到研究问题影响，跨题复用前检查任务适配，不默认把它当纯客观通用资产。
- 发现量按独立论文计，多个有用片段可以保留，但片段数量不提高该论文的相关性权重。目标篇数和调用/时间预算分别记录，不无限翻页凑数。

### 按需细纲加强

当前按需入口采用全池轻导航＋任务相关结构化发现＋完整回读，不再每次发送全池长语义目录。Plus 与 Max 均使用职责投影；Max 只接收已读取的完整记录。实际容量、分步预算与恢复规则见 [按需运行说明](ON_DEMAND_NAVIGATION.md)。以下 4,800 字符语义视图仍是旧完整目录/本地诊断方式，不是当前每次按需调用的全池输入。

- 目录不再取整个 A/B JSON 的前 600 字符。按原语义字段展示，常规预算每材料根 4,800 字符，保留完整记录和发现/条件配对。
- 超长原子内容在否则会成为空视图时允许软预算超出并明确标记；其余省略项列出可读取路径。
- 目录不是完整阅读记录。选中后完整记录进入负责人请求，未选资料仍可通过补读取得。
- 更换目录合同后重新 prepare；不要复用旧 REQUEST 内的 600 字符目录。
- 两组复测入口见 `OUTLINE_MATERIAL_RETEST.md`。

### 案例选择

- 已移除每个科学字符串 1,200 字符的递归截尾。长摘要、发现、限制和工具答案完整进入其来源记录。
- 来源批次仍按现有方式运行；材料投影合同版本已更新，旧截断案例缓存不能静默复用。

### 编排

- 章节主张、单元推进、综合、案例用途与条件完整保留；来源限制不再只取前两条。
- `max_source_chars` 默认 0。正值只缩短导航标签，不能裁掉科学任务字段。
- 老版本的 `0` 可能表示截空，新版明确表示不缩短导航。不要跨版本机械复用参数解释。
- 原 paragraph briefs、finding_conditions、argument_relations 及稳定任务身份继续传递。

### Writer 与补写

- `max_material_chars_per_source` 默认 0；显式正值现在只生成容量提示，不再删除材料。
- 不再因超过 20,000 字符而把补充/工具答案剪成 400 字符。A/B、精读、本地片段及补充材料保留完整内容。
- 正常写作和补写使用同一供材规则。查看 `material_capacity_diagnostic` / `input_capacity`，不是继续把旧 `material_truncated.kept_chars` 当实际保留量。
- 原始材料若已经在更早历史环节被截掉，新程序不会凭空恢复；应从完整卡、packet 或原记录重建请求。

## 输入过大时怎么办

先用实际 messages 计数，再按任务或完整材料记录分批，不能为使请求通过而截掉材料尾部。

- 章节细化已有完整来源记录自适应分批与恢复能力，继续复用。
- 案例已有来源批次；若某批超过模型上下文，保留完整输入，缩小来源/任务批次后重跑该批，不重跑已完成部分。
- 按需加强可以缩小同章可编辑任务簇，同时保留全局/邻近职责；不要把每条来源都独立调用一次。
- 编排、writer 和补写会在实际调用前检查已注册模型的输入与总上下文容量，超窗时返回 `input_requires_batching`，保留完整输入，不产生该次模型调用。计数使用实际 counter，安全余量只加一次。本轮不引入自动多次写作/合并器；超窗任务由运行驱动按既有任务身份分批，不允许自动剪尾或擅自新增付费循环。

不要把字符数直接当 token 数。尽量使用对应 tokenizer；字符估计只能作为明确标记的保守估算。

装得下也不等于读得充分。历史长输入探测中，接近98万token的请求正常结束却只回答了9项中的5项。不要把模型最大上下文当默认填满目标；按真实任务检查中部材料、条件差异和不同路线是否被利用，必要时按完整任务/来源分批。这个历史例子不构成新的固定40万或60万硬门槛。

## 来源身份与工具状态

- P编号属于某次运行的身份空间。跨运行复用按稳定 paper_id、明确DOI等重新绑定，不能因为旧P号当前未占用就沿用，更不能全稿替换某个P号。
- 模型顺序编号不能仅凭数字后缀自动变成本轮P号；Q01等工具标识只作为工具/诊断信息。合法转换须有明确对应关系，未解析时保留原文与问题，不猜论文。
- 明确同一身份的 alias 可共享已有材料；真正矛盾的身份不能合并。多来源综合应保留各来源和章节级关系，不能因为只解析了其中一个handle就归为该论文的单独发现。
- 同论文、同need_id或文字存在包含关系不等于内容重复。互补精读、不同问题、条件和限制分别保留；只有确定相同的内容才去重。
- 同论文的新问题、required_outputs变化时检查旧答案是否覆盖。只有问题列表或“没找到答案”不算 fulfilled；有价值但不完整的回答仍可参与规划。
- 关闭外部检索不应关闭已知论文的本地查询和定向阅读。已知论文身份和阅读问题应能驱动对应工具，不强制用户先补英文检索式。
- 供应商故障、目录冲突、解析失败和当前未命中分别记录，不能被规划器改写为“领域没有研究”。资料不足也不能直接升级为正面证据或永久判为不相关；新材料允许纠正早期判断。
- 抽查工具原结果→负责人实际消息→writer实际消息。正文出现同一个数字可能来自旧A/B，不能作为精读已经传递的证明；本地片段也不能伪装成新完成的全文精读。
- 共享精读额度、独立论文数量、模型调用、历史复用、partial和失败分别统计，不给每章重新分配整份全局额度。

## 实验自主性与评价隔离

运行输入可以包含原问题、原细纲、既有正文、真实材料和通用任务；不可包含本地根智能体先整理的科学错误答案、参考 Max 加强稿或人工预期修改。

自主性检查覆盖实际messages、反馈和重试，不只看顶层输入文件。真实材料可以合法包含“问题”“限制”或 known_issues 等字段；不要用全局关键词黑名单误杀材料。由Agent先指出科学错误再让模型编辑的结果，记录为有指向的修订，不当作系统自主发现能力。

局部任务簇的只读邻近职责必须贯穿细纲加强、编排和writer。chapter_plan只含本批可编辑单元，邻居放只读上下文；局部输入没有某内容，不等于全章缺失。

比较不同模型时固定输入材料、可编辑范围和输出合同。若更换了材料、恢复快照、模式或可见邻居，记录差异。评价集中于机制、比较、条件、阴性结果和任务实际展开，不按引用数、篇幅或表格数量代替质量。

记录每组真正调用的阶段：没有触发强模型升级就标为该机制未测，不据此淘汰多阶段路线。来源校验失败时保留候选，分别判断内容价值与接口问题；不放宽身份规则，也不把一项校验失败等同整份科学内容无用。自动评价先核对候选版本归属，再用于辅助判断。

定位改进应分清：任务是否形成、材料/任务是否送达、正文是否展开。已送达但漏写可用现有补写入口；需撤销错误或重复时使用精确编辑，追加式补写不能代替删除/改正。保留未命中的好段落，不把所有问题退回规划重跑。

## 缓存与恢复

- 恢复必须绑定任务、材料内容、模型与有效参数、提示词/材料投影合同。
- 新参数改变后只看配置文件不够；实际请求不同，旧响应不能当成新配置的测试。
- 已成功章节修订优先保存；新修订失败不退回粗稿覆盖成功成果。
- 格式/导出故障先用已付费原响应离线解析或重放，保留可恢复对象，只重试缺失部分。明确区分重放、重导出和新模型生成。
- 身份或材料污染发生时，定位最早受影响消费者，使其依赖产物失效；保留未受影响的材料和昂贵结果。既不全盘重跑，也不因文件存在就承诺复用。
- 缓存验证包括同目录恢复：同输入零调用、单处变化只重做受影响任务、材料实质变化确实失效。记录实际调用与账本，不只相信“cache hit”日志。
- 按需模块已有阶段checkpoint、完整材料回读及合回完整章包，见 [正式接入](../verification/on-demand-promotion-20261007/README.md)；旧实验阶段仅在结束时写RESULT的限制是历史事故，不再当作当前运行方式。恢复仍需校验实际请求/材料/模型兼容，不能盲目重付费。
- 公开删节视图、脱敏路径、历史汇总文件不能直接作为生产缓存。

## 费用与发布

- 计入实际、reserved 和 uncertain；不清空历史占额换取“剩余预算”。
- 同一共享账本的额度调整必须显式处理；直接从 45 改传 85 会与既有元数据冲突。
- 网络失败不能叠加成“密钥轮换×格式修复”的多重付费重试。区分账户拒绝、传输故障和格式问题，按已授权次数/并发恢复；未确认请求继续保留占额，连接恢复不代表先前免费。
- 模型连接按项目既有直连/代理配置处理，不为修一个供应商擅自关闭系统代理或静默换模型。
- token 估值与供应商实收分开记录，尤其缓存命中折扣。不能只凭输入字数推断账单。
- 分别报告成功链路成本、整个实验成本和失败/恢复费用；耗时分模型处理、超时与调度空等，不把Agent迟迟未处理结果当成方案固有延迟。
- 发布阻塞及时报告。区分代码完成、离线检查完成、远端更新成功；不得一直等接口而让用户反复追问。
- 用远端分支 SHA 再确认发布完成；本地提交或成功创建 Git 对象不等于分支已更新。
- 资料包按实际MANIFEST核对文件是否传齐。远端未上传或公开删节不等于生产链丢材料；原请求、删节视图、重建消息和历史响应重放要分别标明。

2026-10-06材料复测当轮的执行范围以 `OUTLINE_MATERIAL_RETEST.md` 为准：原两单元的完整材料Plus与按需Max小测，沿用新增40元额度；先亲读结果再决定下一步。旧的全细纲推进建议不覆盖后来的暂停和两组小测指令。

## 跨平台与原始资产

- 任务ID可以包含冒号等语义字符，JSON中的身份保持原样；实际目录和raw文件名必须使用可移植编码，防止Windows非法路径或NTFS备用数据流。用临时安全目录绕过失败不等于正式CLI已经修好。
- 字节哈希与统一换行后的文本比较分开记录。LF/CRLF变化不计为科学改进，也不能把归一化后的相等冒称原始字节完全一致。
- 开工先确认本轮实际输入资产存在；历史报告中的本地绝对路径、脱敏占位路径并不是可用材料。缺失时列具体资产及消费者，不用空对象假装补齐。
- 保留本地未提交改动和历史成果；整合云端提交先核对父版本，不整组套旧补丁或强制覆盖远端分支。

## 最小运行检查单

1. 当前 SHA 与本次测试入口相符，输出目录为本轮新目录。
2. 正式规划默认完整 A/B，路由用途未替代材料。
3. 实际模型、思考、回答容量与预期一致。
4. 在真实 messages 中核对代表性长材料的末尾条件与新工具答案仍在。
5. 新细纲任务、条件、来源和身份进入编排及 writer 输入。
6. 恢复时不跳过任务变化后的重要阶段；费用和未决调用完整保留。
7. 科学质量由实际产物亲读评价，不把离线接线测试当作成稿质量分数。

## 可选按需加强的正式接入（2026-10-07）

使用 `scripts/upgrade3/outline_strengthening_pipeline.py` 从完整规划的 writer_packets 清单启动。它串接已有第一层与按需引擎，合回完整章节后导出既有编排所需目录；不要把单组 `ARRANGEMENT_PACKET.json` 当整章。具体命令与验收见 [正式接入说明](../verification/on-demand-promotion-20261007/README.md)。

- 下一消费者明确读取 `CURRENT_PLAN.json` 指向的 `packet_root`；保留未选单元，拆合使用新 ID → 原 ID 列表
- 按需正式调用默认 SSE，单请求 1800 秒、流总时限 3600 秒，思考/回答额度沿用 profile；核对实际执行记录
- 预算分阶段预留，选材完成后按真实 OWNER 请求判断费用；成功 ACCESS 可恢复复用
- 缓存 raw 经过当前消费者重新验证、重新采纳材料。传输参数调整后复用已有答案会记录原执行配置，不伪装为新的传输测试
- 有效前缀和之前接纳快照都保留；预算/网络故障正常恢复。保存的无效结构回答需明确 `--retry-unresolved` 才归档并重试一次，避免无意识付费循环
- 新材料、任务或 scope 变更使用新运行目录；正式复跑仍以完整本地 packet 为输入，公开脱敏消息只作核对材料

## 历史单章写作候选与模型使用顺序（2026-10-07）

以下保留单章实验的运行记录；当前全文候选见下一节。单章入口为 `scripts/upgrade3/writer_candidates.py`，消费已批准章节编排及其对应输入。三条路线是整章一次写、分单元精写后统稿、完整任务分簇后整合。见 [接口说明](../writing_candidates/README.md) 和 [本地60元开工指令](../writing_candidates/LOCAL_AGENT_HANDOFF_60_CNY.md)。

- 写作候选默认 `balanced.json`：Plus写作与Plus编辑。先用Plus把正文写好，Max作为最后底牌；不影响既有上游模块的模型配置
- 提供Flash＋Plus、Plus标准、Plus更多思考、Plus＋Max、全Max参考五种组合。不要把三条路线与五档配置排列组合全部付费跑遍
- Plus更多思考档为32,768思考＋32,768回答；标准为16,384＋49,152。二者总上限均65,536，是用途取舍，不是前者所有容量都更大
- 正式CLI可能付费调用Max时必须明确选择相应配置并加 `--allow-max`；没有自动升级。预览和离线重放不调用模型
- 本轮本地测试总预算60元，共享账本，历史费用和不确定占额保留。先小范围Plus试写，再按实际收益决定下一次调用，不要求花满
- 先核对执行和messages，再评价成稿。任务/材料/参数/选用版本接错，先修接缝；评价不能将这种错误算成写作方案无效
- 精确保留完整任务及相关科研材料，去重不截尾。初稿、编辑稿和最后采用版本可追溯；失败不抹掉此前有效稿


## 历史全文写作候选（2026-10-07，默认地位已被baseline替代）

新的比较与交付单位是已批准细纲中的**完整 BODY**。入口为 `scripts/upgrade3/fullbody_writer.py`，具体接口与本地指令见 [全文候选说明](../fullbody_writer/README.md) 和 [本地100元测试安排](../fullbody_writer/LOCAL_AGENT_HANDOFF_100_CNY.md)。

- 四条路线分别为全文一次成文、带实际前文的连续写作、可完整回看的长文工作台、完整稿的独立读者反馈与精准修订。最后一条复用前三条生成的稿件，避免重复支付初稿费用
- 输入选定的完整规划及所有对应章节编排，程序记录实际文件、哈希、材料与任务身份。第一步离线准备输入清单，不能把单章测试包冒充全文输入，也不要求人工给数百个材料文件逐个写哈希
- 连续作者的后续请求必须带实际已写正文；长文工作台保留原始任务、最近原文及完整回看入口。简单合并独立章节不是连续写作。保存或拼接文件不会自动改善全文论证
- 任务身份属于追踪记录，不要求每个任务生成一个小标题、一段或一个正文块。科学材料决定可支持的表述强度；细纲中的计划性概括不能覆盖相反的材料条件
- 一次写不下时选用相应连续路线，按完整任务调整请求。输入超限与输出触顶分开报告；保留已经生成的有用正文，不能把触顶当成全文完成，也不靠压缩后半篇保住“一次调用”名义
- 默认使用 Plus，保留经济档、较高思考档及显式 Max 升级。所有作者、续写、回看后的继续写作、读者、编辑调用都计入实际路线成本，不设置隐形统稿调用
- 原批准100元已由后续授权下调：本轮高级与新增朴素方案真实实验合计上限为 **40 元**，所有候选共用同一账本。本轮已经发生的费用和未决占额保留；已有100元账本应显式下调到40元，不是在已花金额上另加40元，也不累加旧60元实验余量
- 开发与离线验证可以充分投入，真实测试按已有结果决定下一次调用，不必穷举路线乘模型档位。先检查方案是否落实，再亲读整篇的知识展开、章节推进与条件保留
- 输出同时保留整篇正文、阶段原始返回、实际请求、任务/材料来源、选用版本与恢复信息。正文可用性和局部问题分别记录；小措辞误差不自动触发昂贵重写


## 历史朴素全文基线与40元预算更新（2026-10-07，保留记录）

当前增加 `plain_whole`（普通提示词全文一次写）、`chapter_concat`（独立写完全部章节后直接拼合的基线）、`hierarchical_full`（复用上述全篇初稿后真正全文统稿）。全部使用完整批准BODY输入，不能以单章结果代替全文评价。见 [补测指令](../fullbody_writer/PLAIN_BASELINES_40_CNY.md)。

原100元授权已经替换为**同轮全部方案合计40元**，不是另加40元。已有花费、reserved及uncertain继续占用；不新开40元账本绕开已经发生的费用。旧单章历史试验仍单独保留，不再把其剩余预算相加。

已有本轮100元账本时，先停止旧的付费运行进程，再执行：

```bash
python scripts/upgrade3/lower_budget_limit.py --ledger path/to/current_experiment.sqlite --lower-to 40 --expected-limit 100
python scripts/upgrade3/lower_budget_limit.py --ledger path/to/current_experiment.sqlite --lower-to 40 --expected-limit 100 --apply --report outputs/budget_lowering.json
```

此工具仅下调上限，保留所有调用和占额；不能取消已在途请求。如果实际占用已超过40元，余量记零并停止新调用，绝不抹账。新运行客户端在每次预留时重新遵守存储的较低上限。尚未建立本轮账本时才用统一的新40元账本。

分层路线可通过 `--draft` 复用完整独立章节稿，避免为测试统稿重新支付所有章节写作。其真正全文编辑若因容量/预算未完成，原完整初稿保留，并明确统稿待完成；不要把拼接稿冒充已统稿，也不隐藏额外编辑费用。
