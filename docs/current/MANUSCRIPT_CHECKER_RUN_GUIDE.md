# 完整稿校验与独立出版入口

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

沿用已有预算工厂和Qwen客户端，项目/账户累计封顶与未决占额继续生效，不创建新资金池。默认Plus、16384思考、32768回答空间；profile可配置且进入实际请求，无自动升级。显式Max配置仍需既有 `--allow-max`，只能用于另获授权的运行。本次云端实施不包含任何付费调用。

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
