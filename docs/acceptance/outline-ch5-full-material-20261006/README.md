# Ch5 五单元完整材料 Max 历史结果归档

这是一次独立的历史实验公开归档：第 5 章 5 个可编辑单元、15 个 paragraph tasks，完整相关材料一次发送给 `qwen3.8-max` 负责人。没有 selection/access 调用，实际 owner 调用 1 次，结果为 `updated/stop`。本包不混入 `244b925` 的四单元 Plus access→Max 轮。

## 建议阅读顺序

1. [USER_REQUESTS.md](USER_REQUESTS.md)：本轮归档请求、引用报告与归属。
2. [USER_INTENT_AND_EXECUTION.md](USER_INTENT_AND_EXECUTION.md)：用户要求、本地执行与 root 后验评价的边界。
3. [ARCHIVE_SCOPE.json](ARCHIVE_SCOPE.json)、[SECURITY_SCAN.md](SECURITY_SCAN.md)：版本、范围、脱敏与公开限制。
4. [experiment/ch5_full_material/](experiment/ch5_full_material/)：实际完整材料输入、messages、wire/effective request、raw、RESULT、updated plan、运行报告和 provenance。
5. [reviews/ROOT_CH5_REVIEW.md](reviews/ROOT_CH5_REVIEW.md)：模型返回后的 root agent 后验评价，不是模型输入。
6. [preparation/](preparation/)：cluster/scoped 的离线准备材料和运行脚本；这些请求没有实际执行，不能当作付费调用。
实际目录中的 `experiment/ch5_full_material/DEMAND_ACCESS_MESSAGES.json` 与 `DEMAND_OWNER_UPPER_BOUND_MESSAGES.json` 也是准备阶段上界请求，未执行；实际调用证据以 `FULL_MAX_MESSAGES.json`、`WIRE_REQUEST.json`、raw、`RESULT.json` 和 `RUN_REPORT.json` 为准。
7. [source_snapshot/](source_snapshot/)：受测基线 `a0b645...` 的必要源码文件和原始 dirty patch；当前归档仓库头 `244b925...` 不替代受测版本。
8. [costs/COST_SUMMARY.json](costs/COST_SUMMARY.json)：费用时间点快照；4.522932 CNY 是本次调用，7.401266 CNY 是当时 40 元轮累计使用，不是当前余额。
9. [SHA256_MANIFEST.json](SHA256_MANIFEST.json)：每个公开文件的来源 SHA256、公开副本 SHA256、大小和变换。

## 版本、费用与实际输入

受测代码由 `a0b645e096a148382196b5aadd3dcf36c43c69d6` 加 dirty patch `ea41bced9f47a59af5d80764ce67721d527d0f9ebce9b48ae4e58e94b40c249c` 构成；实际归档所在仓库当前头是 `244b92574f167ad5d25b1040f763963b57a63a18`，不能把当前头当作旧运行版本。`source_snapshot/` 保存基线文件和 patch，方便复核。

实际请求使用 331,422 input tokens、15,163 completion tokens（其中 7,056 reasoning tokens），`qwen3.8-max`，thinking 32,768、answer 65,536、max completion 98,304，finish reason 为 `stop`。本次实际费用为 4.522932 CNY；40 元轮在该时间点累计 7.401266 CNY，属于历史快照，不能推断当前余额。

## 结果边界

实现 checkpoint 保留“31 项测试”的记录；用户交接中提到“32 项”但本包没有补造第 32 项证据。

结果与原计划、实际 request/raw、parsed RESULT、updated plan 和离线 provenance 一并保存。`accepted_into_combined_outline` 为 false；没有正文写作，也没有把 root 后验意见送回模型。root 评价中的收益不足是本轮观察，应与用户方案、模型路线和另一轮 `244b925` 结果分开判断。

## 公开材料处理

JSON/raw 中的 `local_passages`、fulltext 类字段以保留原 SHA256、路径和字节数的标记替代；A/B、模型生成细纲、请求结构、结果和运行元数据保留。SQLite、PDF、凭据、认证头和签名 URL 未纳入。原始输出目录未被修改。
