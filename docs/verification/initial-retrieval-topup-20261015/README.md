# 初始检索不足500篇的补充模块

受测源码：`8029b78dfc3a6710744b5015c830ec14198988e4`。入口和参数见[当前运行指南](../../current/RUN_GUIDE.md#初始m2检索默认有界补足2026-10-15)，全链路位置见[PIPELINE](../../current/PIPELINE.md)。

## 接入合同

正式 `scripts/upgrade3/run_candidate_corpus.py` 默认组合原M2检索器和 `initial_retrieval.execute_initial_m2`：

1. 原检索式执行、稳定身份去重；已有500篇时不创建模型客户端。
2. 不足500时，qwen3.5-plus读取原问题/子问题、已有查询及返回/去重/新增/重叠/传输反馈，自主生成新查询。
3. 追加到同一 `ACTIVE_QUERY_PLAN.json`，标明补充第一、二、三轮；原M1文件及快照保留。
4. 每条新查询后去重，达到500停止剩余新查询，已返回记录完整保留。最多3轮；仍不足或既定预算不够则明确状态并正常交接现有池。

M3继续消费原 `optomind.candidate_corpus.v2`；`INITIAL_RETRIEVAL_HANDOFF.json`固定实际语料、片段库及哈希。没有改后续规划缺口检索或M3材料准入算法。历史私有外部启动器未改写，新调用须使用正式M2接口，不能以旧综合harness代替该入口。

`--preview`免费预览，重复相同目录/输入/账本自动恢复；`--no-initial-topup`显式使用旧单次路径。保存完整RAW和逐查询journal，无法确认是否付费完成的请求不自动再次购买。预算使用原共享账本的累计封顶。

## 独立实测

在原光学初检230篇基础上，三轮补充达到 **230→353→484→500**。生成36条新查询、执行32条、达到目标后4条未执行；原检索式贡献每轮稳定230篇。第一轮及部分查询复用本次已付费/已完成记录，第二、三轮是新生成与检索，不是重新运行整篇综述。

首次实跑暴露元数据限流导致身份桥丢失，已修复为成功元数据持久化及查询前缀视图恢复；失败现场保留。修复后20项专项离线检查通过，源码编译和diff检查通过。

免费恢复新增模型调用0、检索调用0，账本及语料哈希不变；M3的facet消费者能读取补充查询；1440条片段实文哈希一致，未关联论文hit为0。

所有真实试验合计 **0.0449352元**，其中采用链0.0331128元、旧失败试验未采用回答0.0118224元；新增未决费用0。沿原项目80元与账户300元双账记账，不新建资金池。

补充查询拓展了原子问题的相关入口，但题名抽样仍含声学、超声等偏题结果，须经过已有下游相关性筛选。这里500是初检候选，不是合格A/B数量；本轮没有重跑A/B或BODY。

## 本地完整记录

本地根目录：`F:\OptoMind-Review-2\outputs\initial_retrieval_topup_20261015`。

- `ROOT_ACCEPTANCE.md`：亲审、失败修复、费用与接线结论。
- `START_SNAPSHOT.json`：原始输入哈希。
- `live_cached_initial/`：首次失败试验，保留不覆盖。
- `live_identity_fixed/ACTIVE_QUERY_PLAN.json`：累计查询及执行状态。
- `live_identity_fixed/supplement_round1/2/3/`：实际请求、RAW与轮次结果。
- `live_identity_fixed/REUSE_PROVENANCE.json`：复用路径、原始哈希及兼容条件。
- `live_identity_fixed/LIVE_TEST_RESULT.json`：最终结果及账本快照。
- `live_identity_fixed/ROOT_RECOVERY_AND_CONSUMER_CHECK.json`：免费恢复与真实消费者检查。

原始运行包、密钥、账本SQLite和片段SQLite不在这份可公开文档中。完整生产请求本地保留，不能拿此摘要替代缓存。
