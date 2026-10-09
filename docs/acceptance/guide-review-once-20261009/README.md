# 一次 Plus 审查→一次 Max 修订：本地验收

受测源码：`c03490f9b5f16c990123e45a5fb62fa4ebe8e346`。用户目标见 USER_REQUEST.md。

实际结果：Plus正常返回，但六条原文摘录未通过逐字定位，正式状态 response_invalid；Max没有启动，无修订指南和DECISIONS。仅支出1.342212元，未决0元；新30元账本剩余28.657788元。原完整Max指南保留，旧费用未重复计入。本轮不重试，不生成正文。

建议先读 live_once/BASELINE_GUIDE.md，再读 PLUS_REVIEW_RAW_CONTENT.json 与 EXCERPT_DIAGNOSTIC.json，最后看 ROOT_REVIEW.md。重要区分：摘录匹配问题、意见越界到冻结细纲、模型对实际已送达材料的误读。不能因为Max尚未运行就淘汰这条路线。

真实请求及原始返回在 live_once/stages。七章作者免费构造与输入检查在 author_preview_baseline，全章材料输入容量均通过；候选不存在，候选检查未运行。没有付费作者调用。

大消息按原始字节分片；对应 parts.json 给出原文件大小、哈希和片段顺序，拼接即可还原。没有上传凭据、账本数据库、论文PDF、完整输入缓存及重复PAYLOAD副本；实际审查/作者MESSAGES保留。无需重新调用模型即可分析本次行为。

默认仍选择基线；后续修改由云端独立阅读后判断。正式入口默认guide_maker流程未动，测试包装只启用用户授权的欠费换钥。
