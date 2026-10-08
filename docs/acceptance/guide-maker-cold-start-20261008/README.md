# 细纲→写作指南：独立冷启动真实测试（2026-10-08）

本包是新增中间层的本地验收，供云端独立复读。受测基线 **9a5e1d90c70dc102352082a5917a929bf30903a2**；必要的独立账本兼容补丁位于 `verification/IMPLEMENTATION.patch`，关键源码和新增测试见 `source_snapshot/`。这次提交只新增验收资料，不把本地补丁直接并入生产模块。

## 阅读顺序

1. `USER_REQUEST.md`：用户目标、隔离、预算和冷启动限制；测试意图来自用户，具体执行与评价来自本地助手。
2. `CODE_PROVENANCE.json`、`RUN_COMMAND.json`、`cold_start_final/EFFECTIVE_CONFIG.json`：实际代码、命令与参数。
3. `cold_start_final/GUIDE.md` / `GUIDE.json`：唯一真实生成的七章指南。`ROOT_REVIEW.md`为本地亲审意见，建议先独立读指南再读它。
4. `cold_start_final/stages/maker_001/d4f695314ce81c5bbe1ee83ad463f0ee02dc49bba9396ceab807a5236872059b/attempt_001/`：正确的真实请求、原始返回、解析结果与用量。大文本见同名`.parts.json`导航；小文件ACTUAL_REQUEST/USAGE/RESULT可直接读。
5. `cold_start_final/runs/20261008T080543Z-515a3f83b0/`是首次真实调用，`runs/20261008T081626Z-0fb63557fe/`是零调用恢复。顶层CLI_RUN已更新为恢复结果，不要据其model_calls=0误判本轮没真实调用。
6. `writer_preview/CLI_RUN.json`、`WRITER_PREVIEW_PAYLOAD.json`：只做免费作者接口预览，没有付费正文。
7. `verification/`、`BUDGET_FINAL.json`、`PUBLICATION_SAFETY.json`、`MANIFEST.json`：测试、费用、省略项和文件哈希。

## 一页事实

- 真输入七章、29单元、95项任务；来源导航221项、工具导航15项。首次materials为空、prior_guide为空；没有人工A/B指南、旧正文、评价反馈或人工手选科学材料。
- 默认qwen3.5-plus，thinking=true，思考16384＋回答32768，实际max_completion_tokens=49152、stream=true。
- 实际输入213233 tokens，输出7547（usage报告reasoning_tokens=5736）；正常结束。一个真实调用；累计按现有价格计量0.517030元，未结算占额0。新30元账本，与另一个助手的60元任务隔离。
- 模型一次宣告完成，reading_needs为空。**没有发生真实补读**，因此没有补读后的指南变化。程序补读路径经过免费测试，不能当作本轮模型自主补读能力证明。
- 本地亲审认为它有基本章级组织帮助，但仍接近细纲摘要；跨章案例分工、具体解释安排和任务书口吻有不足。请独立核查，不预设同意该结论。
- 受测原版本73通过1个Windows路径失败；修正归档路径分隔符并增加独立预算模式后81项通过。弃用的中间补丁失败记录也保留：`BASELINE_TESTS.log`实际是中间补丁72通过2失败，并非原版基线。原版基线见`SOURCE_BASELINE_TESTS.log`。
- 恢复新增调用0；作者GUIDE合同验证与首章真实请求构造通过，后续章需实际已写前文，本轮未付费构造整篇正文。

## 完整消息与大文件

每个大文件保存为 `<原路径>.parts.json` 与 `<原路径>.parts/001.txt` 等。按清单順序拼接UTF-8字节，不添加分隔符即可恢复原文件；原文件与每段均有SHA-256。没有压缩科学内容或改写实际首次请求。

为了快捷阅读，也可将真实MESSAGES恢复后解析第二条content，查看full_outline/source_catalog/tool_catalog。模型原始返回保留为RAW_RESPONSE的分片；解析结果RESULT.json以及GUIDE.json可以直接阅读。分片只是上传容器，不是对模型输入的裁剪。

## 公开与本地的边界

本轮用户随后授权上传供云端阅读。仓库public，因此不上传密钥、账本SQLite、其身份标记、未明权利的完整科学材料档案、含完整科学材料的作者预览messages或本地完整ZIP。费用汇总、所有原文件哈希、来源身份/DOI及省略清单已附。

首次模型实际接收的是生成的细纲与轻量来源导航，无论文全文，已原字节上传。完整科学档案本轮没有被guide maker模型读取；留在本地，不影响重建首次冷启动请求。`MATERIAL_IDENTITIES.json`提供它的身份/字段/哈希索引；`PUBLICATION_SAFETY.json`列全部省略项。

本地完整测试包：`F:/OptoMind-Review-2/outputs/guide_maker_acceptance_20261008_30cny/`。原文件未改；ROOT_REVIEW中的“未公开上传”描述的是完成本地验收时的状态，本包是后来获授权的公开阅读副本。
