# 运行来源与恢复方式

用户原始授权与执行者的选择分别记录在 `records/USER_SCOPE.md` 和亲审记录中。评价中的科学意见从未交给候选写作者或编辑器。

## 锁定来源

- 云端候选实现：`fe2f1c2adde414e71341287845d0520325e602c6`。
- 格式修补：`14257f4274e1deeb77aa6d125447bfb2a5d4c7d3`，仅恢复完整 JSON 回复中的未转义内引号。
- 超时修补：`32772fbe1a1d3de0969f4d2e27b0fd5cf725b939`，分别执行900秒无数据超时与3600秒整体流超时，不再以后者覆盖前者。这两处均不修改提示词或科学内容。
- 完整章节编排：`on_demand_promotion_local_20261007_10cny/arrangement_paid/Ch2/CHAPTER_ARRANGEMENT.json`，SHA-256 `c9a7ebafefc253a211ab0c6b5345cee5acaa66e30d490e7638e40b6bce8e40ad`。
- 配套材料视图：同目录 `ARRANGEMENT_INPUT.json`，SHA-256 `66d1dd3e7d696eb6d8c8d634721202610b42261f39576d0aa1c5d7d0df997472`。
- 公共输入规范哈希：`11ca4978141ad642d8cab038a286855e1fce29cc05b5ede87998199473000270`。这与 JSON 文件字节哈希是两种不同记录。
- 实际输入为四个完整单元，包含两个已加强单元和两个保留单元，并非以两单元局部加强结果冒充全章。

## 正式入口

从锁定源码根目录运行 `scripts/upgrade3/writer_candidates.py`，不使用临时作者或编辑脚本。本地实际参数如下，尖括号项需指向本地保留的原件：

```powershell
C:\Anaconda\python.exe -X utf8 scripts/upgrade3/writer_candidates.py `
  --arrangement '<批准的完整章节编排>' --view '<配套完整材料视图>' `
  --route chapter --config config/writer_candidates/balanced.json `
  --tokenizer '<既有本地tokenizer.json>' --output '<独立A目录>' `
  --run --budget-ledger '<同一共享budget.sqlite>' --key-file '<本地凭据文件>'
```

路线 B 仅改 `--route units_edit` 和输出目录。其四次单元写作后接正式编辑器；编辑器依据实际初稿重新构造请求。没有重新运行规划、检索、精读或首尾模块。

qwen 通过生产 `QwenDirectClient` 直连；流式返回，单次请求/读取超时900秒、整体流超时3600秒，自动重试0。balanced 两个角色均为 `qwen3.5-plus`、思考16384、回答49152，实际发送 `max_completion_tokens=65536`。各阶段保留 wire 参数与 provider usage，未把配置上限当成实际使用量。

本地分词器 SHA-256：`5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`，tokenizers 0.23.2。预留输入使用实测 token ×1.12，加8192余量；真实用量另行记录。

## 预算与恢复

启动时保留90条历史账目，其中已结算77.5919498元、旧预留9.421812元、旧未确定8.7471504元。新60元建立在原95.7609122元暴露之上，账本总上限155.7609122元。按启动 reservation-ID 集合的差集统计本轮；没有释放旧占额或每路线新建额度。费用是生产客户端依据实际 usage 写入共享账本的数值，原始 provider 用量同时保留。

恢复相同正式请求时读取已完成阶段，不重新付费。改变编辑配置时应保留相同写作配置、源码和实际输入，复用成功初稿，只有编辑阶段产生新调用。不要删除 raw 或忽略未确定费用来重启。

A 首轮 HTTP 成功且正常结束，但模型 JSON 文本有六处未转义的内引号。原返回和首轮 pending 文件都保留；修复后离线解析仅插入六个转义字符，解码后的正文未变。`runs/chapter_live/format_repair/` 明确连接原付费请求与恢复版，恢复不是第二次模型生成。

B 首次运行只有首个单元完成。第二个请求先长时间没有数据，随后在监督停止前收到部分流，但没有DONE、finish_reason或usage；该稿不计为完成。停止后把当前请求1.826132元保留为uncertain，既不写成0元，也不释放原占额。修正无数据超时传递后，Bv2在独立目录以同一科学输入重新运行；原首个单元的0.065972元仍计入总费用。

## 编辑器升级与最后恢复

balanced Bv2 运行 `20261007T124218Z-4c3f3755a1`；Plus reasoning 编辑运行 `20261007T130126Z-5772883350`；最终 Max 编辑运行 `20261007T132115Z-0dbf5cd0ff`。编辑升级沿用同一 `units_edit_live_after_timeout_fix` 目录，保持四个作者配置和源码相同，实际全部复用作者缓存。相同请求的 cache 标识必须从 live mode 计算，不能用 preview mode 的另一个标识判断是否复用。

三次实际编辑消息文件 SHA-256 都为 `3ac15c8291e7f4059a70717398089b251d6e635203e9cefc6017441d53e1b884`。Plus reasoning 的 32,768 思考＋32,768 回答实际生效，但只删除一个引用；因此才决定使用后备 Max。没有把本地评价交给编辑器。

首次 Max 运行 `20261007T131101Z-a2fe9d97db` 的进程消失，既没有最终响应，也没有确定送达证据；不能推断零收费。5.411664 元保留为 uncertain，相关原报告和更正报告分别保存。最后一次恢复使用受监督的正式 CLI 会话，保持 32,768 思考，显式将回答容量从 65,536 换为 32,768，使包含旧占额的 Max 最坏占用低于 10 元；完整材料未变。保留该实际配置，而非把它写成原 selective_max 配置。

成功 Max 的 request ID 为 `chatcmpl-15e0731d-22a8-9836-a040-834ffd710c05`；132,073 prompt、20,270 completion（含14,455 reasoning），2.314596元。总新增费用见 `records/FINAL_BUDGET.json`。启动时按历史 ID 保存快照，最终仍使用同一账本差集，不释放未知费用。

## 公开投影与私有原件

公开包包含完整任务、章级职责、论文身份和已有模型生成 A/B 理解摘要，实际模型返回、初稿、选用稿、参数、用量与评价。消息文件名明确为 `MESSAGES_PUBLIC_PROJECTION.json`：精读/补充及可能含论文原文的字段以哈希和省略说明代替，不能拿公开副本声称逐字重放。

本地原始请求、SSE、材料快照、账本快照和恢复脚本仍在 `F:/OptoMind-Review-2/outputs/writer_candidates_local_20261007_60cny/`。`MANIFEST.json` 记录公开件字节哈希、原件哈希和每项省略。密钥、认证头、论文PDF/XML/HTML全文及完整SQLite账本不公开上传。
