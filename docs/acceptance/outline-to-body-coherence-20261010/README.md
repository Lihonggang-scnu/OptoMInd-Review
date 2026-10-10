# 选定现版并正式接入“细纲→正文”

本轮结论：**采用现版 baseline，正式默认启用任务落实核查、有界补写和一次全文局部编辑。** 新增的 chapter_coherence 上下文在一处改善了分工，但没有稳定减少跨章重复，另一个单元遗漏任务后需要追加补写，阅读顺序反而变差。没有清楚净收益，按用户决策规则结束比较，不再追加抽样。

## 阅读顺序

1. [ROOT_REVIEW.md](ROOT_REVIEW.md)：根智能体亲读十份采用稿后的判断、具体收益与退步。
2. [BASELINE_ADOPTED.md](BASELINE_ADOPTED.md)、[CHAPTER_COHERENCE_ADOPTED.md](CHAPTER_COHERENCE_ADOPTED.md)：固定五单元的两组实际正文。
3. [PAIRED_DIFF.md](PAIRED_DIFF.md)：逐单元差异；两组都为新生成，不是旧稿与新稿混比。
4. [COSTS.json](COSTS.json)、[SOURCE_VERSIONS.json](SOURCE_VERSIONS.json)：本轮物理调用费用、原项目余额、实际受测与最终接入源码。
5. [FORMAL_DEFAULT_PREVIEW.json](FORMAL_DEFAULT_PREVIEW.json)、[FORMAL_FULL_CACHED_VALIDATION.json](FORMAL_FULL_CACHED_VALIDATION.json)：正常入口默认值、完整稿免费恢复证据。
6. [EVIDENCE.zip](EVIDENCE.zip)：实际请求、返回、原始/采用正文、质量处理、历史失败尝试、运行与验证脚本；逐文件索引见 [EVIDENCE_MANIFEST.json](EVIDENCE_MANIFEST.json)。

旧七章完整测试及原始证据仍保留在固定提交
`02167134f3eb8a9e88e3fb98812e9a2ec6f2da95` 的
`docs/acceptance/outline-to-body-20261010`，本包不重复上传旧证据压缩包。
[SELECTED_FULL_BODY_HANDLES.md](SELECTED_FULL_BODY_HANDLES.md) 与
[SELECTED_FULL_BODY_READER.md](SELECTED_FULL_BODY_READER.md) 是该已认可完整稿本次正式选用的原样副本，不是本轮重新生成的七章。

## 测试了什么

原完整输入有7章、29单元、92个段落任务和3个表格任务。本轮只重写五个代表单元：Ch1/U4、Ch2/U03、Ch5/U01、Ch5/U04、Ch6/U3。两组使用同一完整输入、材料、任务和模型容量；原任务与材料保持。旧科学残余不是本轮必须清零的清单，不向模型提供人工科学问题、答案或旧成稿。

| 角色 | 模型 | 思考额度 | 回答空间 |
|---|---|---:|---:|
| 作者/有界补写 | qwen3.5-plus | 8,192 | 32,768 |
| 单元核查、补写后核查、全文局部编辑 | qwen3.5-plus | 16,384 | 24,576 |

两组子集均启用核查与最多一次有界补写，不运行全文编辑。采用决定同时读初稿与最后采用稿，不把少写、字数下降或引用数下降当成去重收益。详细请求和实际有效参数在证据包内。

baseline 受测源码锁定 `73f938e40ec6c46947d5cca34ad0eee542631107`；候选实际受测为 `1e01821f9dfa6959859e3815d116f3723d4e7699`。首次候选因研究路径的 economy ceiling 在发送前被拒绝，费用0；已修正正式 BODY 入口的模型策略接缝，保留失败尝试后恢复。其余没有质量重抽。

## 正式产品入口

当前开发入口：`lihonggang-dev`。

```powershell
# 免费预览；不传质量开关时即采用正式默认。
python -X utf8 run_review_harness.py --delivery-start body `
  --delivery-input FULL_BODY_INPUT.json --delivery-out outputs/my_body `
  --tokenizer LOCAL_TOKENIZER.json

# 在同一命令后添加以下参数，允许真实调用。
--run --ledger OWNED_PROJECT.sqlite --budget-limit ABSOLUTE_LIFETIME_CAP `
  --budget-scope PROJECT_ID --key-file LOCAL_KEY_FILE --key-index 1
```

也可用 `--delivery-manifest PREPARED_BODY_MANIFEST.json` 替代 `--delivery-input`，正式入口沿既有 manifest 导出器取得细纲、编排和材料，不重跑规划、不生成指南。产物位于 `<delivery-out>/body`：实际选用句柄稿、编号读者稿、引用目录和 `DELIVERY_REPORT.json`。`selected_body` 给出实际采用路径；`effective_settings` 给出生效版本、模型与质量步骤。

默认流程：原分单元作者 → 单元任务核查/必要时一次补写与后核查 → 既有装配 → 一次全文局部编辑 → 选用实际编辑稿并编号。编辑未产生可用稿时保留装配稿；未完成作者与质量阶段仍按既有状态报告。首尾和出版不会在未传 `--delivery-config` 时自动运行。

恢复使用**同一输入、版本、输出根、模型容量、账本、scope与累计上限**重跑命令。成功响应及原始返回优先复用，失败尝试保留；没有自动反复付费重试。确需重试已有失败时显式添加 `--retry-failed`。`--reparse-saved` 可免费重新解析已保存的质量返回，缺失步骤需要原预算允许的真实运行。不要换目录或账本重新起算预算。

回退选择：`--body-version baseline` 回到所选正式版本；原历史独立 CLI
`scripts/upgrade3/legacy_unit_writer.py --quality-control --article-edit`
保持可用，并走相同产品运行库。若只需原作者与装配，显式使用
`--no-quality-control --no-article-edit`。实验候选只在显式
`--body-version chapter_coherence` 时启用，使用独立输出目录；不会自动替换正式版本。

详细接口与入口控制见 [LEGACY_UNIT_QUALITY.md](../../writing_candidates/LEGACY_UNIT_QUALITY.md) 和 [PIPELINE.md](../../current/PIPELINE.md)。完整已认可稿的原目录通过同一产品库复用验证：29/29单元、187个编号论文身份、原始响应和采用正文不变、0新增调用/预留。保存的 pending 科学诊断仍在报告中，CLI保留相应非零状态；没有为了交付手填完成状态或清除诊断。

## 费用、版本与边界

沿原60元独立项目账本；启动结算12.0901596元、占额0。当前精确支出与余额见 COSTS.json，项目与第一密钥账户总账为同一次调用的双重约束，不能相加。历史独立 Max 上探费用另列，本轮 Max 调用0。按实际物理调用及本轮开始时间归属，不重复计算旧请求指纹相同的历史调用。

本轮固定对照已结算：baseline 12次调用，1.7923108元；chapter_coherence 13次调用，3.6834704元；合计5.4757812元。项目累计17.5659408元，未决占额0，剩余42.4340592元。正式接入预览与原稿恢复均为0元。

本轮免费接线包含实际正常 main/子进程、模拟流式传输与真实测试账本控制，以及本机完整输入预览、已有整稿恢复；新生成质量比较只有五单元，不冒充又重跑七章。原始请求与响应完整保留，公共包排除真实凭据、账本数据库及原论文全文/PDF。

保留当前已认可的知识展开与引用广度。候选暂不默认使用；剩余局部科学措辞与来源判断记录留存，不影响本轮选定版本并正式接入的完成。
