# 队列状态

本状态对应 2026-10-03 云端工单交接。

## 当前状态

首轮只授权 `00` 和 `01`。01 后必须写出 `READY_FOR_ASTRA_01`。`02`–`07` 排队等待用户明确授权；授权由 root 转发到实施线程，并应点名允许的工单。

| ID | 状态 | 闸门/依赖 | 停止条件 |
| --- | --- | --- | --- |
| 00 | AUTHORIZED | Package entry; read master plan and cloud baseline | `00` record complete; no algorithm change |
| 01 | AUTHORIZED | 00 record and baseline mapping | `01` record complete, then `READY_FOR_ASTRA_01` |
| 02 | WAITING_APPROVAL | 01 后用户明确授权 | 授权前不工作 |
| 03 | WAITING_APPROVAL | 02 完成后用户明确授权；或授权明确点名 02–03 | 授权前不工作 |
| 04 | WAITING_APPROVAL | Explicit approval after 02–03 | No work before approval |
| 05 | WAITING_APPROVAL | Explicit approval after 02–03 | No work before approval |
| 06 | WAITING_APPROVAL | Explicit approval after 04–05 | No work before approval |
| 07 | WAITING_APPROVAL | Explicit approval after 06 | No work before approval |

## 状态转换规则

- 工单只有在闸门明确开启后才能进入 `IN_PROGRESS`。
- 只有按 `RECORD_TEMPLATE.md` 完成记录，工单才能进入 `READY_FOR_REVIEW`。
- 首轮必须停在 `READY_FOR_ASTRA_01`；检查通过不会自动启动后续工单。
- 检查失败时记录失败并回到 `IN_PROGRESS`，不会扩大授权范围。
- `07` 只有在 06 后才能报告短链准备情况；它永远不授权完整综述或付费运行。

## 交接必须包含

root 审阅需要工单记录、生产差异摘要、fixture/短链输入和输出、正常路径对照、未解决项及实际命令或 harness 入口。不得附凭据、原始秘密或本地绝对路径。

## 2026-10-03 WO-03 explicit continuation

User message `Sentinel_7c82dc03c35481919a5bcc4c06830ff7` authorizes only WO-03 from acceptance commit `4bb82be54d8d812c2d11b1e3a45e8ff863af0ee4`. The earlier table is historical. WO-00–02 have bounded local acceptance; WO-03 is READY_FOR_ASTRA_03 after 212 bounded offline tests passed (2 full-fake-chain controls deselected), on independent branch `body-chain-repair-cloud03-20261003`. WO-04–07 remain WAITING_APPROVAL. Stop after WO-03 at `READY_FOR_ASTRA_03`.
