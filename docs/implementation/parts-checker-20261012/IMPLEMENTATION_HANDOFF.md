# 两项实施交接：首尾写作与完整稿校验

基准：`1981c116faaf7f27ad0fd5bb6d849b1bf8b93975`，工作分支 `lihonggang-dev`。本轮为代码、离线响应回放及实际消息验收，没有项目付费调用。

## 两项提交与取舍

1. 首尾独立提交：`aadb629f3a6a9972cb511127a9d4393185dcd8f1`，`Improve independent post-body manuscript writing methods`。三段具体写作任务、可选writing_requirements、完整提示合同哈希及独立测试/消息证据。保持四阶段顺序、职责卡、standalone装配和BODY作者。第一项在不含第二项代码的隔离基准副本上191项测试通过。
2. 校验与交接提交：`Add snapshot-bound manuscript checking and selected-draft delivery`，实际SHA见交付包清单或该分支提交日志。新增严格局部提案执行、最多两次逻辑调用、失败项S1回读、真实材料依据、缓存/收据、独立及正常共同消费者。任务一和任务二分别提交，未合并成不可区分的一次修改。

采用LOCAL_REVIEW的范围：有当前稿或所给材料依据的数值、公式及引用纠正允许执行。新增最小kind/evidence_ids字段，不把所有改动伪装成editorial。程序提供稿内证据ID及材料ID；没有额外科学评审模型或自动检索。合同证明定位与依据来源，不证明科学蕴含。

三项具体调整：
- 候选段序改为可调整的信息动作；没有固定段数、每段引文配额或统一“无引文就失败”硬门槛。
- 二轮默认明示为首次检查＋一次失败修复；网络物理重试另记，不增加第三逻辑轮。跨块依赖修改首版留待办；有据独立局部修正正常执行。
- 实查发现04在没有DOI/paper_id时会把不同namespace的同P号合并。小范围修正既有唯一引用映射的fallback身份为namespace＋来源句柄，保留有强身份时跨namespace去重，避免checker正确执行后在编号时丢失身份；新增三项专门回归。校验器另外覆盖同目录行handles别名及DOI/paper_id强身份别名，修复后不放宽跨namespace弱身份授权。

## 正式入口

首尾沿用 [PARTS_IMPLEMENTATION.md](PARTS_IMPLEMENTATION.md) 的 `scripts/upgrade3/manuscript_parts.py`。实际四阶段消息在 [parts_evidence](parts_evidence/PARTS_ACCEPTANCE.json)；两个异领域样例完整保留，历史长稿使用明确标注的缩略预览和原稿引用，可由 `preview_parts.py` 免费重建完整消息。

校验与独立出版使用 [正式运行指南](../../current/MANUSCRIPT_CHECKER_RUN_GUIDE.md) 及 `config/manuscript_delivery/post_body.json`。新入口消费已经生成的03报告，不隐式重跑BODY或首尾。

以下变量替换为本机真实路径。首次使用新输出目录；账本始终是原项目和账户的原账本。配置要使用对应身份目录、显式材料及待插图注。

免费消息预览：

```sh
python scripts/upgrade3/manuscript_delivery.py --parts-report "$PARTS_REPORT" --config "$DELIVERY_CONFIG" --output "$NEW_OUTPUT"
```

免费响应回放：

```sh
python scripts/upgrade3/manuscript_delivery.py --parts-report "$PARTS_REPORT" --config "$DELIVERY_CONFIG" --responses "$SAVED_RESPONSES" --output "$REPLAY_OUTPUT"
```

本地正式运行（本轮云端没有执行）：先不带`--run`及key参数预览，再运行下列完整命令。

```sh
python scripts/upgrade3/manuscript_delivery.py --parts-report "$PARTS_REPORT" --config "$DELIVERY_CONFIG" --output "$LIVE_OUTPUT" --live --run --ledger "$PROJECT_LEDGER" --budget-limit "$PROJECT_LIFETIME_CAP" --account-ledger "$ACCOUNT_LEDGER" --account-budget-limit "$ACCOUNT_LIFETIME_CAP" --account-mapping-dir "$EXISTING_MAPPING_DIR" --incremental-budget "$APPROVED_INCREMENTAL_CAP" --tokenizer "$LOCAL_TOKENIZER" --key-file "$PRIVATE_KEY_FILE"
```

关闭新校验阶段：

```sh
python scripts/upgrade3/manuscript_delivery.py --parts-report "$PARTS_REPORT" --config "$DELIVERY_CONFIG" --output "$DISABLED_OUTPUT" --no-manuscript-checker
```

关闭不会读取密钥、构造模型或创建新校验阶段；明确选择03原稿，记录disabled，不宣称已通过校验。

恢复：完整重复原命令，使用同一代码、输入、profile、输出目录与原账本。已完成RAW/收据恢复零新增调用。明确的无响应传输异常重试，在原live运行命令增加 `--retry-failed`；有不确定流或已有响应不会重新购买。禁止删除绑定、换目录重置轮数或复制账本扩大预算。输入/提示合同变化应开新比较目录并继续原生命周期账本。

需要PDF时增加 `--compile-pdf`，使用本机已有完整TeX环境；默认先交付Markdown和元数据。

## 实际消费者证据

[DELIVERY_ACCEPTANCE.json](delivery_replay/DELIVERY_ACCEPTANCE.json) 来自真实CLI免费回放，未手填阶段完成标志：

- 03原稿SHA：`a7e795cd71f078fc91f6f5121fdaabae13bbf0a3a09868dcd51617a0909d3c79`
- checker选中候选SHA：`207f06d1e850b78d8c7ca58a28e177713a0507c8f56c9e3af0b8589889099f76`
- 04编号输入是该候选的明确书目投影，SHA：`1184d585dede5498080338a1d7c1c1a6bfa2f1be1ae147ffbdeb1f834af08911`
- 04实际reader与05出版Markdown SHA相同：`5ea07091fa365e93d5f3f4411503bbef484f778d77eb9f5b42c8f8d7013c876e`
- 合成摘要去除一个重复词后，05 `PUBLICATION_METADATA.json`确为“本文解释比较条件。”，证明没有读取旧03摘要。
- 同目录再次运行：`resume_cache_hit=true`，`resume_model_calls=0`。

正常 `review_delivery.run_downstream_delivery` 与独立CLI都调用 `manuscript_delivery.run_manuscript_delivery`。05的 `MANUSCRIPT_SELECTION.json` 带实际路径、哈希、来源、applied/pending/unresolved及图表映射诊断。缺选用稿或SHA不符停止，不能glob最新文件或静默换回03。

归档中的绝对路径是当次运行位置，不能直接当作另一台电脑的可执行输入。`preview_delivery.py --output NEW_DIRECTORY` 会用当前环境新路径重新生成同类免费证据，已有目录拒绝覆盖。

## 验证结论与未跑项

- 第一项隔离验证：191通过。
- 最终相关回归：375通过、11项明确排除；另有13项仓外独立对抗验证通过。覆盖实际提示分阶段、BODY字节/CRLF保护、单块唯一定位、重叠全挂起、部分成功S1迁移、科学计数/符号、材料支持修正、namespace、引用定界符、恢复、关闭、预算预览与真实出版消费者。
- 11项依赖未入库的20260927旧稿/REFERENCES、旧unit manifest或plan输出。先运行已确认FileNotFoundError，未用其他稿假冒这些输入，也未修改这些历史测试以伪造通过。具体名称和结果见 [VALIDATION.json](VALIDATION.json)。不宣称全仓测试通过。
- 原生项目依赖下运行，没有在最终结果中隔离或伪造生产包导入。离线验收使用禁止socket连接的进程保护，没有项目付费调用、项目账本或密钥访问。
- 额外调用真实`--compile-pdf`验证了TeX生成及“失败不假成功”：当前云端虽有xelatex可执行文件，但缺`xelatex.fmt`、格式配置及TeX宏包，编译未得到PDF，协调结果为restricted。完整Markdown与元数据链已通过，PDF编译留本地完整TeX环境复核；不把本轮写成PDF已验收。
- 真实写作质量、自主问题发现、证据是否充分及实际费用仍待本地固定BODY与模型的小额质量测试。离线fixture仅证明合同与接线。

跨领域测试的科学内容只存在于合成fixture，不进入通用生产提示词。历史driver、历史产物、main、原BODY作者、凭据和项目账本均未修改。GitHub推送状态与本地提交身份分别列在交付包清单，不能以本地commit代替远端写入成功。
