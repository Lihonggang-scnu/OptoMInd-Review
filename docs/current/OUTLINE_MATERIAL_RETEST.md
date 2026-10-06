# 完整材料 Plus 与按需 Max 复测入口

本次优先交付只修复按需目录的材料可见性。原细纲、真实材料、模型职责、正式 BODY 顺序和正文保持原样。规划默认值、编排和 writer 的其他输入损失修复另行提交，不必等待它们才能进行本次两组细纲测试。

## 固定输入

- 使用本地保存的同一组原两单元 payload，含同一原细纲、完整相关来源/工具材料、只读邻近职责、身份映射和相同旧正文背景。
- 不使用公开删节归档替代生产材料；不把上轮 Max 加强答案、人工科学问题或评价放入请求。
- 本例完整材料是 29 份相关来源＋2 份工具材料，不是 585 篇库存。
- 每组使用新的输出目录和独立 call_id；历史结果只作评价对照。

## 先离线准备

```text
python scripts/upgrade3/outline_strengthening.py --mode single --profile autonomous_outline --input <本地原payload.json> --output <新目录/full_plus> --tokenizer <本地tokenizer.json>

python scripts/upgrade3/outline_strengthening.py --mode on_demand --profile strong_outline --access-profile autonomous_outline --input <同内容的本地原payload.json> --output <新目录/on_demand_max> --tokenizer <本地tokenizer.json>
```

`autonomous_outline` 为 qwen3.5-plus；`strong_outline` 为 qwen3.8-max。两者思考与回答容量均为 32,768，实际请求总回复容量为 65,536；目录访问也使用显式 Plus profile。容量不是必须用满的长度。

需要保持旧实验无损去重方式时，两组之外不要随意改变材料语义；完整材料组可沿用已核实的 `--deduplicate-materials` 传输方式，但必须保存投影和完整展开一致性结果。

## 本次目录修复

- 取消整个 A/B JSON 只取前 600 字符。
- 使用原材料语义字段形成定位视图，优先呈现问题/范围、发现与条件、规划用途、限制。
- 每个材料根的常规展示预算提高至 4,800 字符，保留完整字段或完整记录，不剪半句话或拆散发现与条件。
- 对不能安全拆开的长原子字段允许明确标记的软预算超出；不把唯一可用的长工具答案替换为空值。
- 省略内容仍有路径可请求；选中的完整记录及续读取材保持原样。
- 目录合同进入 hash。旧 `REQUEST.json` 必须重新 prepare，不能直接沿用旧 600 字符目录。

## 模型调用前检查

1. 完整 Plus 的最终 messages 包含完整相关材料；不是只在旁边的 payload 文件里保存了材料。
2. 按需目录实际出现核心发现、对应条件、planning_summary 和限制；不能只比较目录字数。
3. access 与 owner 的实际请求记录区分：目录中可定位不等于完整卡已读取。记录初选与补读轨迹。
4. 模型名、thinking、thinking_budget、max_completion_tokens 以 effective_request 为准，配置文件值只是输入。
5. 沿用本地已批准的新增 40 元预算和共享账本，包含本轮结算、保留额及未决额；不要清空历史记录。先完成这两组小测，再决定全细纲。

准备命令默认不调用模型。真实运行在对应命令加 `--run`，并使用本地现有受控驱动的账本/密钥参数。当前 CLI 不负责把已有账本额度从 45 自动变为 85；不要仅修改命令参数造成账本冲突。现有按需 CLI 的阶段恢复仍需使用本地已核实的 checkpoint 驱动或独立保存响应，不得盲目重跑已付费阶段。

## 内容验收

系统自主修订后亲读：比较与案例职责是否更具体，条件和阴性结果是否保住，是否减少无用重复，是否引入新的错误；来源校验和内容质量分别记录。不得把人工发现的问题回填给受测模型补考。

费用同时报告实际 usage、当地账本估值与缓存命中；供应商账单与估值可能不同。两组没有必要花满预算，不重写正文。
