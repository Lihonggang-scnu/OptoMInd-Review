# 完整稿校验与独立出版入口

**2026-10-12用户已验收并接受交付。** 受测源码b98771f2cc1548c301f6621d0b87414d858d9f43；[真实稿件、请求、返回、费用及恢复记录](../acceptance/parts-checker-delivery-20261012/README.md)。已知局部瑕疵保留诊断，不阻断已接受初稿。本文是当前接缝和操作说明；10月11日的准备说明是历史。

新入口 `scripts/upgrade3/manuscript_delivery.py` 消费已经生成的03完整句柄稿报告，不重跑BODY或首尾。它与正常 `review_delivery.run_downstream_delivery` 共用 `runtime.upgrade3.manuscript_delivery.run_manuscript_delivery`：03 → checker → 显式 selected_manuscript →04编号 →05出版。历史验收driver和产物不改。

## 配置与输入

复制 `config/manuscript_delivery/post_body.json` 到本轮配置；补充 `identity_catalogs`、必要 `post_body_context.material_records` 和待插入的 `figure_assets`。身份目录可为列表或 `{"path":"REFERENCES.json"}`；路径相对配置文件。上下文中的 `writing_requirements` 原样传给首尾消息。checker始终看实际完整稿及明确提供的材料，不自动搜索。

`--parts-report` 可是独立首尾的 `LIVE_REPORT.json` 或 `FRONT_BACK_REPORT.json`，必须成功并包含实际 `final_manuscript`。相对稿件路径以该报告所在目录解析；跨机器的旧绝对路径需要本地明确映射，不按最新文件猜测。完整目录通常还需沿用首尾使用的 `IDENTITY_CATALOGS.json`，在配置中显式提供。

新的post_body配置默认启用checker；legacy模式保留默认关闭。关闭配置为 `"manuscript_checker":{"enabled":false}`，CLI也支持 `--no-manuscript-checker`。无模型输入时启用阶段生成真实消息预览并停在checker，不静默选03出版。

## 免费预览与回放

```bash
python scripts/upgrade3/manuscript_delivery.py \
  --parts-report PARTS/LIVE_REPORT.json --config delivery.json \
  --output outputs/manuscript_check_new
```

不带 `--live --run` 不构造真实客户端、不读取密钥。消息/快照在 `03_manuscript_checker`；预览尚无响应，后续04/05不会启动。

```bash
python scripts/upgrade3/manuscript_delivery.py \
  --parts-report PARTS/LIVE_REPORT.json --config delivery.json \
  --responses recorded_responses.json --output outputs/manuscript_replay_new
```

响应文件用 `{"responses":{"round_1": <完整响应>, "round_2": <必要时的修复响应>}}`，也可直接提供轮次映射。它是显式免费response replay，不伪装为新模型生成或精确匹配新提示词的历史运行。模型输出里的snapshot_id必须来自本次实际原稿，修复只返回本次pending ID。普通delivery配置也可写 `"manuscript_checker":{"fixture":"responses.json"}` 或 `recordings`，相对配置文件读取。fixture输入是顺序响应列表或一个changes对象。不要手填“已完成”状态替代真实阶段。

## 本地预算受控运行

先执行同一live命令但不加 `--run`，预览并绑定既有预算；阅读消息和估算后才追加 `--run --key-file PRIVATE_KEY --key-index 1`。

```bash
python scripts/upgrade3/manuscript_delivery.py \
  --parts-report PARTS/LIVE_REPORT.json --config delivery.json \
  --output outputs/manuscript_live_new --live \
  --ledger EXISTING_PROJECT.sqlite --budget-limit ORIGINAL_PROJECT_CAP \
  --account-ledger EXISTING_ACCOUNT.sqlite --account-budget-limit ORIGINAL_ACCOUNT_CAP \
  --account-mapping-dir EXISTING_DUAL_CALLS --incremental-budget AUTHORIZED_INCREMENT \
  --tokenizer LOCAL_TOKENIZER.json
```

沿用已有预算工厂和Qwen客户端，项目/账户累计封顶与未决占额继续生效，不创建新资金池。默认Plus、16384思考、32768回答空间；profile可配置且进入实际请求，无自动升级。显式Max配置仍需既有 `--allow-max`，只能用于另获授权的运行。原云端实施不包含付费调用；随后本地真实验收的9次调用及费用见采用记录。

首次 `CHECKER_LIVE_BINDING.json` 固定原稿哈希、配置、profile、tokenizer、双账本与增量基线。同目录恢复重复完全相同命令；不删除binding、响应或收据，不换目录重置预算。保存的成功RAW/SSE免费回读，已完成checker结果按其收据恢复；缺失新调用仍需 `--live --run`。

显式传输失败重试：原命令追加 `--retry-failed`。只有已记录异常、无可回放内容且无SSE的尝试可新增物理attempt；原失败文件和占额保留。格式/定位修复属于最多两次逻辑调用中的第二次，不以传输重试增加第三轮。含不确定流或已有响应的失败不会自动重买。

关闭示例：去掉live预算参数，使用 `--no-manuscript-checker` 与新的输出目录；选03原稿并记录disabled，不宣称校验通过。

## 消费者与输出

- checker的 `selected_manuscript` 包含path、sha256、status、provenance；缺路径或哈希不符停止。
- 04只消费选中稿或其明确旧书目投影；05只消费04的reader。
- 05 `PUBLICATION_METADATA.json` 从该reader提取真实题名/摘要/关键词。
- 05 `MANUSCRIPT_SELECTION.json` 以及 `DELIVERY_REPORT.json` 携带checker applied/pending/diagnostics、选用来源和图表映射检查。
- `MANUSCRIPT_DELIVERY_REPORT.json` 是本轮独立协调报告。
- `--compile-pdf` 走既有离线出版器；需要本机TeX环境。无该参数只生成Markdown及元数据，不声称产生PDF。

04后检查未知引用、残留图号token和未插入图片。checker能看明确提供的待插图注，但不宣称校验了未提供图注；缺图/锚点等诊断保留给本地处理。完整稿带未决诊断可交付，执行成功不是科学质量验收。真实质量由本地亲读与授权预算测试。


## 阶段文件接缝：以报告选择为准

| 交接 | 生产者与应读取的字段/文件 | 消费规则 |
|---|---|---|
| BODY→首尾 | 正式BODY `DELIVERY_REPORT.json.selected_body.handles_draft`、`references_path`；问题/范围/全文主张与最终细纲属于同一run | 输入句柄稿，不能拿BODY数字读者稿做新引用母稿；不重跑作者或再执行BODY全文编辑。身份目录及显式材料随同传入 |
| 首尾内部 | `serial_manuscript_parts.py`：CONCEPTION/MANUSCRIPT_PARTS_PLAN → conclusion → introduction → abstract；每步保存messages/response | 后一步接实际已解析部件。职责卡是本篇后置构思，不复用历史并行规划卡；段落数组只做无损表示转换，不能人工补正文 |
| 首尾→校验 | `FRONT_BACK_REPORT.final_manuscript`；独立live时在`LIVE_REPORT.runtime_report.final_manuscript` | 必须是成功装配出的实际稿。相对路径按传入报告目录解析，跨机绝对路径显式映射；原BODY装配投影保留。新结语在文章参考文献前 |
| 校验→选择 | `03_manuscript_checker/CHECKER_REPORT.json.selected_manuscript`，path/sha256/status/provenance；round_N的EXECUTION_RECEIPT/FEEDBACK | 每轮绑定输入快照、指定块及逐字旧片段；应用同一原快照的非重叠区间。第二轮只处理pending，绑定当时新稿，不能把旧第二轮锚点移植到另一个candidate |
| 选择→04 | `run_manuscript_delivery`校验selected路径与SHA；必要时另写`04_NUMBERING_INPUT_HANDLES.md`，保存投影前后SHA | 只在独立投影中移除旧书目，保留owned首尾；先挂图再按最终阅读顺序统一编号。`04_figures_citations/STAGE_REPORT.json.selected_manuscript`保留原选择；不按mtime、glob或固定03路径猜稿 |
| 04→05 | 04实际reader_draft/references_path/图映射/assets | `run_publication_delivery`只消费04，不重新挂图或编号；PUBLICATION_METADATA从04 reader提取题名/摘要/关键词，不能从旧BODY上下文覆盖 |
| 最终交付 | `MANUSCRIPT_DELIVERY_REPORT.json`＋05 `DELIVERY_REPORT.json`/`MANUSCRIPT_SELECTION.json` | 总报告和sidecar串起checker诊断、selected SHA、04投影及05消费出处；MANUSCRIPT_PUBLISHED.md是本次Markdown交付物 |

## 默认、状态和恢复不要混淆

- 正式BODY baseline、核查及一次全文编辑的默认值不变；独立首尾仍显式启用，`front_back.mode=post_body`时新校验默认开启。`--no-manuscript-checker`明确选03稿并记录disabled。
- 独立live的默认校验参数为qwen3.5-plus、16384思考＋32768回答（max_completion_tokens=49152），流式；没有自动换Max。上限不是目标长度。
- 完整选用稿`complete_with_diagnostics`可以进入04/05，pending及unresolved继续保留。首轮无有效响应、没有选用文件或SHA不符，不能继续。第二轮中断且首轮有效时保留首轮选用稿和待办；未解决项不伪装完成，也不重买已成功响应。
- `manuscript_parts.py --live --run`同目录复用持久缓存并在新的attempt原子装配；普通config fixture/recording核心的03非空保护仍保留。两种入口的恢复范围不同。
- `manuscript_delivery.py`的live同目录恢复使用相同源稿、配置、tokenizer和预算binding；离线responses/fixture恢复使用CHECKER_IDENTITY及响应哈希，没有live预算binding；成功响应/最终收据免费恢复。本次新运行0调用只指这次恢复，历史首次计数看冻结报告。
- 首尾生成新的attempt后如果final_manuscript路径/SHA变化，已有checker binding不可直接沿用；应明确使用新校验输出目录/阶段绑定。此前已付费材料及旧选用稿保留，不能删binding绕过兼容检查。
- live只在`--run`才调用；免费预览没有响应时停在checker，不直接给03盖“已校验”。格式/定位最多两轮逻辑调用，传输重试按原失败记录和预算规则，不能无限重抽。
- 原累计项目/账户封顶及本次固定增量基线共同生效，已结算＋reserved/uncertain计入。项目/账户是同一物理支出的两种约束，费用不能相加；阶段delta也可能包含交错任务，逐请求账单以真实reservation为准。

## 本轮已经核对的具体文件

本次源BODY→03→checker→04→05的每个哈希见[FINAL_ACCEPTANCE.json](../acceptance/parts-checker-delivery-20261012/FINAL_ACCEPTANCE.json)。从各轮收据重构的全文与实际选用稿逐字一致；187个论文身份保持，04 reader与05出版稿字节一致。同目录恢复见[CACHE_RESUME_EVIDENCE.json](../acceptance/parts-checker-delivery-20261012/CACHE_RESUME_EVIDENCE.json)，0新增调用/费用/占额。

本地已完成段落数组兼容、数字/结构编号的格式等价、带注释书目定位和第二轮失败保留等通用修复。实际首尾/校验产物由用户亲读接受；具体质量问题保留在技术亲审。继续开发应改善通用构思、定位与检测能力，不能把这个样例的数字或科学答案写进规则。
