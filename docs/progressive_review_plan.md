# 渐进式综述规划层

这层使用已经完成 A/B 阅读的论文卡片，交付完整写作细纲和按章整理的素材包。目标是把分类、取材、比较和章节安排尽量做在写作之前。

## 流程

1. 一级规划员阅读全量 B，提出初步主线和必要的工具需求。
2. 一级集中补充与定向精读并行执行；有结果后，根据实际所得完成一级大纲。
3. 面向共同大纲，分批阅读全部 B，逐篇提炼具体章节用途，再统筹章节范围、比较和衔接，合并二级工具需求。分批覆盖全部卡片，不以少数精读论文代替全量素材安排。
4. 二级集中补充与精读执行后，再更新共同章节安排。
5. 各章读取逐篇用途、原卡片中的研究条件说明和精读要点，形成具体细纲；完整 A/B 留在各章写作素材包，避免大上下文掩盖中间的论文。
6. 对全篇做局部完善，再把有实质贡献的补充研究安排到具体内容单元。具体用途与案例才算取材，候选名单长度不能代替细纲质量。

每一级只有一次集中补充窗口；一批可以包含多个明确缺口。工具内部继续复用已有的多通道检索、材料获取和 A/B 阅读。找到部分材料或未找到时，规划员根据当前材料调整设计，不不断重新开需求单。

定向精读默认参考额度为 40 篇，可通过 `--deep-read-limit` 调整。所有章节和两个层级共用额度。同一论文的任务尽量合并；已有解析可以作为输入复用。综述转述的原始研究保留同等使用价值，不因原研究全文未重新获取而降低权重。

## 使用

先预检，不发出模型或检索请求：

```powershell
python scripts/upgrade3/progressive_review_plan.py --preflight `
  --pool outputs/planning_support/20260923/practical_refresh/supplement_live/PLANNING_POOL.jsonl `
  --plan outputs/upgrade3/CROSSDOMAIN_REWORK/X1_microbiome_ICI/PLAN.json `
  --output-dir outputs/progressive_review_plan/20260924_microbiome/run01 `
  --topic-id microbiome_ici_review `
  --budget-ledger outputs/review_blueprint/20260922_phase1/budget.sqlite `
  --budget-limit-cny 35
```

真实运行将 `--preflight` 换成 `--run`，增加 `--key-file api_keys/qwen-api-key.txt`。`--stop-after level1` 或 `--stop-after level2` 可在阶段结束后查看内容，随后使用相同参数和 `--resume` 继续。默认章节并发 3、精读并发 3，可分别调整。

`--prior-reading` 可重复指定本题已经完成的定向精读结果。不要把其他题目的阅读材料混入本题。

当前采用 Qwen 3.5 Plus 思考模式完成全局构思、取材分配和协调，Qwen 3.7 Flash 完成阅读与按章细化；通过既有直连接口调用。`--chapter-model` 可单独调整细化模型。费用进入指定的同一份总账。上面的 35 元是本次实验既有授权总额，不代表新题目自动获得预算。

本地分词器为 `data/tokenizers/qwen3_5_9b/tokenizer.json`。它估算上下文，并为托管模型差异及输出预留余量；真实 token 用量以服务返回为准。不会为了装入上下文而静默截掉论文。

## 交付

- `DETAILED_REVIEW_PLAN.md`：供人阅读的完整细纲。
- `DETAILED_REVIEW_PLAN.json`：结构化规划结果。
- `writer_packets/`：各章的细纲、对应 A/B 和详细材料。
- `stages/`：已完成规划阶段，可在继续运行时复用。
- `level1/`、`level2/`：工具调用产物。
- `CALL_COST*.json`：各次运行前后的共享费用状态。

细纲的内容单元应说明具体认识、展开顺序、案例、比较综合、必要条件和衔接。论文用途由内容决定，允许一篇用于多个章节；约 150 篇以上是材料充分时的长篇综述目标，不通过塞入无关论文凑数。

模型输入只携带一次有效提取内容及其中实际使用的参考条目，完整阅读产物仍留在本地。一次有限的补充搜索未找到某类论文，只能说明当前材料不足，不能据此断言该类研究不存在。

这是独立的升级模块入口，尚未替换旧 `run_review_harness.py` 的整条生产链。真实测试结果另见相应运行目录；离线测试不等于模型内容质量通过。
