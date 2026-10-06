# 第一层真实调用前检查点（离线）

状态：`prepared_no_paid_calls`。当前源 HEAD 为 `e7a1d835ccee9430558596d76f77343f663a3942`，工作树干净；请求来自原始 7 章/29 单元输入，输入 SHA256 `8c609a41af3079d26cb70de2e26ee70db6ff1716b1efe7efd92e1651b09a0bd5`。没有把 root 后验评价、旧候选答案或人工科学问题加入请求。

最终请求：[SELECTION_REQUEST.json](SELECTION_REQUEST.json)，文件 SHA256 `44e52a36fbfc9d11cd6b90bc9321d0ddc168eac1dfefbc65e56884a0b22122cc`；实际 messages 规范 SHA256 为 `35e6c803e9bd18e91d4a4f7835828953ca9bd3d3c7784ea118f2da4fb8fc4dab`。system/user 字符数分别为 `1160` / `176824`。profile 为 `qwen3.8-max`，thinking `32768`，answer `32768`，`max_completion_tokens=65536`。本地 tokenizer 估算 prompt `80025`，保守输入 `97820`，预留 `3.533136` CNY。

模型可见投影保留 7 章/29 单元的完整 chapter_plan；同章边界明确，跨章可编辑组合关闭；提供 `199` 条稳定身份导航、`244` 个回读指针和 `11` 组路径。默认模式是 plan-referenced navigation-only，消息明确未读取不代表材料不存在，并由下层负责人回读完整记录。

第二层三个文件在 `b56cba83436ebf76208807ab31bb569afb611fb6` 与测试 HEAD 的 git tree bytes 完全一致，见 [SECOND_LAYER_BYTE_CHECK.json](SECOND_LAYER_BYTE_CHECK.json)。针对性离线测试为 88 passed、0 failed、0 paid calls，见 [LOCAL_TEST_RESULT.json](LOCAL_TEST_RESULT.json)。

预算只读快照见 [BUDGET_PRECHECK.json](BUDGET_PRECHECK.json)：历史 ledger 保留 settled `57.229208`、reserved `9.421812`、uncertain `0.213722` CNY；本次新批准 40 CNY 轮在本检查点 spent/reserved/uncertain 均为 0，首调用估计后余 `36.466864`。准备阶段未修改 ledger，未启动第二层。
