# 独立首尾与全文校验：已验收交付（2026-10-12）

**用户决定：已亲读测试稿，判定合格，允许交付。** 本目录保存已接受的真实结果、此前技术亲审及原始输入输出。早先ROOT_REVIEW中的“首尾暂不宜定版”是用户验收前的技术建议；本次采用决定以本段为准。已记录的局部瑕疵继续保留，不因此重跑、换稿或阻断交付。

正式源码：`b98771f2cc1548c301f6621d0b87414d858d9f43`。本次归档/文档提交在该源码之上，不代表又生成了一轮正文。

## 先读什么

1. [正式接缝与运行说明](../../current/MANUSCRIPT_CHECKER_RUN_GUIDE.md)：当前操作入口、阶段文件合同、状态、恢复与关闭。
2. [最终实际出版稿](live_checker_after_seam_fix/05_publication/MANUSCRIPT_PUBLISHED.md)。
3. [实际校验选用稿](live_checker_after_seam_fix/03_manuscript_checker/round_1/CANDIDATE_HANDLES.md)与[新首尾装配稿](live_parts/attempts/attempt_003/MANUSCRIPT_FINAL.md)。
4. [真实首次执行总报告](final_live_snapshot/MANUSCRIPT_DELIVERY_REPORT.json)、[费用与选择审计](FINAL_ACCEPTANCE.json)、[零调用恢复](CACHE_RESUME_EVIDENCE.json)。
5. [技术亲审](ROOT_REVIEW.md)、[首尾细读](ROOT_PARTS_REVIEW.md)、[本地通用修复](../../implementation/parts-checker-20261012/LOCAL_FIXES.md)。亲审只评价模型产物，没有向生产请求输入人工科学修法。

## 采用的链路

```text
DELIVERY_REPORT.selected_body.handles_draft + 对应身份目录
  → 后置构思职责卡 → 结语 → 引言 → 摘要/关键词/题名
  → FRONT_BACK_REPORT.final_manuscript（LIVE_REPORT.runtime_report包裹）
  → 全文校验员，最多两轮严格JSON局部修改
  → CHECKER_REPORT.selected_manuscript（path + sha256 + provenance）
  → 04最终阅读顺序编号及参考文献
  → 05仅消费04的reader/REFERENCES/图映射/assets
```

首尾是独立正文后模块，保持原BODY；校验员的局部修改另有收据。BODY既有全文编辑不在下游重复运行。post_body配置默认启用校验员，首尾/下游仍需显式调用；不将整个上游假称为一个默认命令。

## 本轮实际运行与版本

| 项目 | 本轮事实 |
|---|---|
| 云端实现 | 首尾aadb629f3a6a9972cb511127a9d4393185dcd8f1；校验/交付09d37bb45485b4112fa3de94d17382ec84fb2414，原bundle提交导入 |
| 本地修复 | b9853618e0c5c4bf5ac452ead339c1676d5815bc；b98771f2cc1548c301f6621d0b87414d858d9f43 |
| 原BODY | `inputs/SELECTED_BODY_HANDLES.md`，sha256 `8ef37120cc8842cb06d5a583a135e136676e4d778221d14f7c068cc1d26a8245`；7章29单元，未重跑 |
| 当前03装配 | attempt_003，sha256 `2b1413ae7ac3869ce8a603b465fbc3ce572aa769011d48b098d5cbf2f6437125`，结语在带注释的参考文献节前 |
| 当前校验选用稿 | round_1，sha256 `27d85daba6ef624cebab1293fa99787b7dcb953b61e1896eb3100b621aab8472`；8项已应用、2项待办、1个未决问题 |
| 引用及出版 | 187个论文身份，未知句柄0；04读者稿与05Markdown出版稿字节一致 |
| 模型 | 全部直连qwen3.5-plus，思考16384＋回答32768；实际参数在REQUEST及RAW记录 |
| 费用 | 首尾5物理调用0.3978208元；旧快照校验2调用0.5355012元；当前校验2调用0.5210812元；总1.4544032元，占额0，20元剩余18.5455968元 |
| 恢复/回放 | 五份首尾响应免费重装配、旧校验响应免费重解析、正式同目录恢复0新增调用；分别记录，未混称新生成 |
| 免费回归 | 12个相关测试文件448通过；包含异领域合成控制、正式CLI、选用稿与消费者；本轮未新增异领域付费试验 |
| PDF | 本轮compile_pdf=false，仅交付Markdown；原49页PDF是历史结果，不能标作本轮重新验收 |

首次校验输出保存在live_checker/；数字/标题格式保护修复后的旧返回重解析保存在checker_first_response_free_replay/。最终选用的是live_checker_after_seam_fix/，不是把两次读稿修改手工拼到一起。

## 文件接缝与恢复定位

- 首尾真实输入：`inputs/SELECTED_BODY_HANDLES.md`＋`inputs/INPUT_CONTEXT.json`＋`live_parts/IDENTITY_CATALOGS.json`；原BODY选择声明见`inputs/BODY_DELIVERY_REPORT.json`。
- 首尾每阶段实际请求/返回：`live_parts/cache/<stage>/MESSAGES.json`、`REQUEST.json`、`RAW_RESPONSE.json`等（具体文件名以目录为准）；职责卡及最终部件见`live_parts/attempts/attempt_003/`。
- 校验每轮：`live_checker_after_seam_fix/03_manuscript_checker/round_N/`保存实际消息、RAW、执行收据和反馈；所有applied变更可从输入快照逐字重建。
- 04路径、旧书目投影及SHA：`live_checker_after_seam_fix/04_NUMBERING_INPUT_HANDLES.md`和04的`STAGE_REPORT.json`；03及校验选用稿不被投影覆盖。
- 05出处：`live_checker_after_seam_fix/05_publication/MANUSCRIPT_SELECTION.json`记录选用稿和消费者来源，不能直接拿03原稿或旧BODY读者稿替换。
- 原执行计数见`final_live_snapshot/`；同目录恢复后的报告model_calls=0，表示这次恢复没有购买，不代表历史没调用。
- 本目录在.gitattributes中关闭文本换行转换，保证Git提交/跨平台checkout后快照、RAW及执行收据的字节哈希不变。原始报告里的本机绝对路径保留以便复核，不伪造跨机器可运行性。复制到另一台机器时，先按本目录对应相对文件显式映射报告/config路径，再免费预览。`verify_acceptance.py`和START脚本是保存的本机运行记录，使用前需映射其本地路径及既有账本；不是产品硬编码要求。
- ORIGINAL_LOCAL_MANIFEST.json保留原本地文件定位和哈希记录；其中原README对应本包LOCAL_ACCEPTANCE_README.md，原绝对路径按上述inputs映射核对，不把本次新增采用说明的README冒充旧哈希文件。省略重复transport/SSE流及账本数据库，RAW_RESPONSE已完整保存；没有复制密钥文件。不要据归档说明重新创建或清零历史账本。

## 已接受的残余问题

引言仍有逐章导航，摘要与结语有重复；构思卡的一处结果压缩混并传到结语。校验员修复标题等局部问题，但未识别全部内容错误，两轮也没修好一个逐字定位失败。用户已读实际产物后接受本轮交付；这些记录用于下一次通用改进，不是继续付费或手改这篇稿的命令。

## 本次提交与发布范围

用户本次指令为“我查看过，是合格的，允许交付。做好说明并提交，尤其注意各个文件的接缝”，沿用本对话对同一仓库lihonggang-dev交付推送的授权。只正常快进发布该分支，不修改main或archive/history，不改写历史。旧OptoMind-Review-2的9月29日本地冻结pre-push与push URL设置仍保留；本次发布用单次命令覆盖hook目录、明确目标push URL和已有代理，不永久解锁或改全局Git设置。

归档193份文件已逐一核对Git暂存blob与磁盘原件字节相同；新增资料和导入代码的凭据检查无命中。核心实际选择及恢复证据继续用FINAL_ACCEPTANCE/CACHE_RESUME_EVIDENCE，不伪造新的模型运行。
