# 第二轮证据供材全文写作候选

从七条真实路线的结果出发，新增四条显式可选路线；保留旧入口和全部上游算法。

- [七路线独立复核与取舍](SEVEN_ROUTE_REVIEW.md)
- [本地新增60元执行指令](LOCAL_AGENT_NEXT_ROUND_60_CNY.md)
- [离线实际消费者与原始响应重放](INTEGRATION_EVIDENCE.json)
- [验证与限制](VERIFICATION.md)

入口：`python scripts/upgrade3/evidence_body_writer.py --help`

输入：批准的完整BODY manifest、对应细纲/编排/packet、A/B/精读/工具材料；精修另外接收一份完整底稿。

输出：`FULL_BODY.md`、`FULL_BODY_RESULT.json`、实际每次 `MESSAGES/REQUEST/RAW_RESPONSE/RESULT/USAGE`、材料编译档案、选择轨迹、任务状态和费用记录。正文完成与编辑完成分开；失败保留有效稿和未决项。

路线：`packed_whole`、`packed_continuous`、`dossier_author`、`scoped_revision`。默认Plus，作者/编辑16384思考＋49152回答；读者/策展16384＋24576。输入不按字符截尾。按真实当前请求分步预算，不按“全池每次重读”虚构最坏费用。

本轮四条路线共用新增独立60元账本。此前高级60元、朴素40元是历史两笔独立预算，不能和本轮混用。旧 `docs/fullbody_writer/` 中合并40元的预算文字已过时。
