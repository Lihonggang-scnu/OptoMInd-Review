# BODY 案例链恢复记录

日期：2026-10-02

本轮用户决定优先于 `docs/REVIEW_V2_FROZEN_BASELINE.md` 中较早的冻结说明。冻结文档第 72 行附近描述的“案例建议进入 `case_suggestions`，再由章节负责人采纳”决定已被撤销；后续 Agent 不应据此把当前实现改回 `cases → owner` 顺序。

`--planning-revision` 的 BODY 链恢复为旧成功顺序：全局协调与负责人章节修订先完成，然后案例组直接写入正式 `supporting_studies`，再进入章节编排和正文写作。全局协调与负责人修订仍复用现有 `_post_case_review` helper 的职责；生产链不再在案例附加后等待负责人确认。案例使用字段为 `contribution`，案例只有在随附真实 A/B 或精读材料存在时才进入正文计划；用途是写作任务，不是论文结果。

本次改动保留 A/B、真实材料、单元上下文、capacity 和 overflow 的现有通路，也保留用户审定的正文职责约束。恢复旧案例提示词的全篇约150–200篇明确用途文献目标，属于选材广度指导，不是硬性验收数量；禁止为凑数纳入无关论文。案例缓存合同已更新，不能把旧建议缓存当作新提示词的生成结果。

Root 用当前五章 `stages/chapters` 的真实细化结果及真实 `case_groups` 返回进行零调用回放。原先未交给写作者的 `CH01_U02` 九条案例全部进入正式细纲与编排视图；再使用明确标注的受控编排夹具，确认九篇的真实 A、B 和各自用途全部抵达 writer 实际消息。检查位于运行根目录 `case_chain_restore/root_actual_cases_check/ROOT_CHECK.json`。这证明材料交接恢复，不证明新模型编排或正文质量已经通过。没有把旧稿答案用作生产输入，没有覆盖历史产物。

历史对照为2026-09-26的 `run585`。其运行记录未保存完整代码SHA，不能断言运行时精确提交；其阶段顺序、正式案例字段与当日提交 `edeafbed68d7c6cbd7734d638b9c8973a850ac58` 一致。此次恢复的是该案例执行顺序和交接方式，并非把整个代码仓库退回该提交。

针对性回归覆盖案例链、BODY 边界、章节 capacity、材料复用、transport retry 和 planning feedback scope；当前结果为 53 passed。
