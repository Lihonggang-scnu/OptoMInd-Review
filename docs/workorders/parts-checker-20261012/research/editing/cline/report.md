# Cline 局部编辑执行协议源码核对

## 结论

对 OptoMind 的“长中文 Markdown 精确局部修改”，Cline 当前的 `editor` 可作为轻量字符串替换执行器的参考；**不宜直接复用当前 `apply_patch` 的匹配策略**。也无需引入整套 IDE、Agent loop 或 `@cline/core`。

最有判别力的差异：

1. `editor` 使用 `old_text/new_text`，要求匹配一次，除换行转换外不做 trim/fuzzy/Unicode 规范化。它更接近精确编辑的基础，但重叠出现计数、版本校验、批量原子性、幂等都需补齐。
2. `apply_patch` 的所谓“exact”已先做 NFC、引号、破折号/数学减号、特殊空格和部分转义规范化；即使 `fuzz === 0`，也不代表原文逐字相等。后续还会自动接受相似度 ≥0.66 的第一个候选。
3. Parser 失配时保留已解析 chunk 并产生 warnings；**真实 executor 的上层会把任何 warnings 升级成错误，写盘前整体拒绝**。不能只读 parser 就声称实际支持失配部分成功。
4. 匹配/解析失败可在写盘前拒绝，但多文件最终写入是逐文件执行，无事务回滚；第二个文件写入失败时，第一个可已改变。重试必须先确认实际状态。

## 1. 锁定版本与范围

- 仓库：cline/cline；默认分支由 GitHub REST 返回 `main`。
- 固定提交：[`8eba11b2703f5b12d3f415544be37ffc0ca03be1`](https://github.com/cline/cline/commit/8eba11b2703f5b12d3f415544be37ffc0ca03be1)。
- 提交时间：2026-10-11 23:46:46 UTC。
- 读取时间：2026-10-12 05:29–05:34 UTC；版本定位来自 GitHub 插件实际 API 读取，不用搜索摘要作源码证据。
- 当前主路径是 `sdk/packages/core/src/extensions/tools/`。当前工具工厂的真实名为小写 `editor`、`apply_patch`；本文不把旧版 `replace_in_file` / SEARCH-REPLACE 资料当作该提交实现。
- 已保存 24 份源码/清单/原测试，逐个按 Git blob SHA 校验原始字节，全部匹配；见 `source-verification.json`。源码未改动。
- 已读系统提示、schema、tool wrapper、executor、parser、对应原测试、换行助手、模型路由与 VS Code 预览委托。未运行整 IDE、完整 SDK suite、模型回合或生产编辑。

[默认工具名](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/constants.ts#L12-L22)；
[工具工厂选择](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/definitions.ts#L914-L963)；
[VS Code 最终委托 SDK 写盘](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/apps/vscode/src/sdk/sdk-diff-edit-coordinator.ts#L60-L87)。

## 2. 实际协议、schema 与提示词

### editor

真实 payload 示例：

```json
{
  "path": "/workspace/doc.md",
  "old_text": "第一段。\n\n第二段。",
  "new_text": "第一段修订。\n\n第二段。"
}
```

Schema 要求 `path` 非空字符串、`new_text` 字符串；`old_text` 可省略/null；`insert_line` 可省略/null，为可强制转换成整数的一基行边界。执行器优先选择 insert；无文件则创建；现有文件缺 old_text 时返回明确恢复提示，要求读文件、提供原文或改用 insert，且不要原样重发。

工具描述要求小而精确的编辑，并建议已知不重叠区域可同一 response 发多次 editor call。Schema 文案说 old_text 必须恰好一次匹配。old/new 各有 6000 JavaScript 字符长度上限：schema 文案称 recommended，但 wrapper 实际超限返回失败。默认超时 30s，`retryable:false/maxRetries:0`。

[Schema](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/schemas.ts#L194-L224)；
[6000 限制常量](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/schemas.ts#L7-L10)；
[超限拒绝函数](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/helpers.ts#L20-L32)；
[描述、执行包装与失败结果](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/definitions.ts#L698-L753)；
[执行分支/缺原文恢复提示](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/editor.ts#L221-L267)。

### apply_patch

JSON schema 为 `{"input":"补丁字符串"}`，wrapper 也接受直接 string。补丁不是标准 unified diff；语法包含：

```text
*** Begin Patch
*** Update File: doc.md
@@ 可选节锚点
 上下文
-旧行
+新行
 上下文
*** End Patch
```

支持 Add/Update/Delete/Move，多文件、多 hunk。提示词要求用上下文定位、不要行号，重复块可用 @@；shell wrapper 兼容但不推荐。无 sentinel 时 executor 会补齐；有 sentinel 但不完整时拒绝。

默认 preset 使用 editor；模型路由在 act 模式对 openai-native provider 或模型名包含 codex/gpt 启用 apply_patch、关闭 editor。因而不能只依据“Cline”产品名推断某次真实调用用了哪一种。

[apply_patch 描述](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/definitions.ts#L606-L642)；
[schema/执行 wrapper](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/definitions.ts#L649-L690)；
[路由](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/model-tool-routing.ts#L60-L74)；
[输入规范化](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.ts#L79-L142)。

系统 act prompt 同样要求先取上下文、可并行独立任务、最后验证文件。这些是提示约定，不是执行器的文档版本校验或批量事务保证。
[系统提示](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/shared/src/prompt/system/act.ts#L15-L24)。

## 3. 匹配与定位的真实语义

| 维度 | editor | apply_patch |
|---|---|---|
| Exact | 将 old_text 换行转换为文件 EOL 后做 JS 字符串匹配 | 先 canonicalize 再比较整段；不是真正 literal exact |
| Trim | 无 trim fallback | 逐行 trimEnd，fuzz+1；逐行 trim，fuzz+100 |
| Anchor | 无独立 anchor 参数；把上下文放进 old_text | @@ 文本从当前游标向后找首个规范化/trim匹配行，命中后游标移到下一行；未找到不报错 |
| Fuzzy | 无 | Levenshtein 归一相似度 ≥0.66 自动接受，fuzz+1000 |
| 重复 | 非重叠出现多次即拒绝；重叠计数有缺口 | 每层从起点找第一处；不检查唯一性；fuzzy层也非全局最佳 |
| 跨段 | 多行字符串可跨空行、段落 | line-based hunk 可含多个空行和段落 |
| Markdown结构 | 不理解 | 不理解 |

关键源码链：

- editor 的 countOccurrences 为 `content.split(needle).length - 1`，它只数不重叠出现。`哈哈哈` 中搜索 `哈哈`，实际上起点0/1皆可，但 count=1，执行器接受第一处。应改为逐起点枚举全部 occurrence，并明确重叠也计数。
  [计数](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/editor.ts#L67-L70)；
  [替换与错误](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/editor.ts#L162-L191)。

- apply_patch canonicalize 会 NFC；把 U+2212 数学减号与多种破折号变成 ASCII '-'；弯引号变直引号；NBSP/窄不换行空格变普通空格；去掉反引号/引号前部分反斜杠。
  [完整 canonicalize](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch-parser.ts#L58-L83)。

- findContext 顺序为规范化相等 → trimEnd → trim → 相似度0.66。规范化相等返回 fuzz0。exact层遍历整段，所以后方 exact 会压过前方 fuzzy；但进入 fuzzy 层后，返回第一个过阈值者，不选最高相似度。
  [完整分层](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch-parser.ts#L347-L430)。

- @@ 只移动搜索起点，不是有范围终点的 Markdown section。不存在的锚点无显式失败。重复锚点也取第一处。
  [anchor定位](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch-parser.ts#L194-L225)。

- `*** End of File` 首先从文件尾部候选尝试，若失败仍可从正常起点重试并 fuzz+10000；不是强制 EOF 断言。
  [EOF回退](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch-parser.ts#L421-L427)。

规范化仅用于定位，不会直接把全部文件的数学符号改成 ASCII；问题在于“不相等原文也获授权匹配”，而非声称其会全文件改写标点。applyChunks 以 origIndex 和 delLines.length 切片，写入前并不重新比对待删文字。
[applyChunks](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.ts#L162-L192)。

## 4. 批内顺序、失败、恢复与幂等

### 同一个 apply_patch

- 同一路径重复 action 会被拒绝。该路径的全部 hunk 都基于同一份加载原文定位，不会把前一 hunk 的输出当后一 hunk 的新输入。
- 每次成功定位后游标跳过全部 old/context 行；后续 hunk 必须在其后。上下文重叠也可能导致失配，哪怕真正修改区不重叠。
- applyChunks 拒绝 origIndex 倒退/重叠和越界。
- parser 对失配 hunk记录 warning并继续；但 computePatchChanges 发现任何 warning就抛 DiffError，因此正常执行入口不提交这些“剩余成功块”。
- 全量计算 changes 后，applyChanges 按文件顺序 write/rm。此处没有原子多文件事务、自动 rollback、compare-and-swap 或最终版本重验。只保证计算阶段失败时未开始写盘；不能把它升级成磁盘提交的全有或全无。

[重复action](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch-parser.ts#L148-L152)；
[逐hunk游标/警告](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch-parser.ts#L227-L258)；
[warnings提升为异常](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.ts#L374-L394)；
[顺序写盘](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.ts#L316-L365)。

### 多个 editor call / 重试

editor 一次仅一个 replace/insert，没有 edits[] 批协议；该执行器内未实现同文档锁、base revision/hash、op_id 或结果收据。工具描述允许同response发多个不重叠 call，不等于存储层批量原子性保证；本研究未宣称整个Agent调度器的所有并发模式均已验证。

两种工具都关闭自动重试，但这只是调度标记，不是幂等保证。精确替换若 old_text 在 new_text 中仍存在，重放可重复添加；insert_line、context插入补丁也可重复插入。成功返回丢失或超时之后不能原样重发。withTimeout 是 Promise.race，没有取消原始写操作：超时错误并不证明没有改动。

[重试与超时参数](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/definitions.ts#L653-L673)；
[editor状态性重试说明](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/definitions.ts#L702-L716)；
[超时实现](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/helpers.ts#L48-L58)。

## 5. CRLF、Unicode、数学与表格

- 两个 executor 默认 UTF-8。editor 的替换回调用 `() => normalizedNewStr`，避免 JS replacement string 的 $&、$$、$' 等特殊展开；LaTeX 的美元符号和反斜杠可字面保留。
- editor 除 old/new 换行转换外不改 Unicode 标点，不做 NFC。未匹配区域按原字符串保留。insert_line 则 split/join 整个文本，对混合换行会全文件归一。
- apply_patch 读文件时 CRLF→LF，计算后按原文件是否出现任何 CRLF恢复 EOL。均一 CRLF正确保留；混合 CRLF/LF会全部归成 CRLF，包括未编辑段。这对“字节级只改指定区”不合格。
- Markdown行尾两个空格可能有硬换行含义；trim层会忽略它们。全角/半角、表格列、代码块、数学块无语义验证；多个相似表格行/公式应视为歧义，不能凭0.66相似度自动应用。
- Unicode canonicalization、UTF-16字符长度/距离、Markdown换行含义都说明“肉眼相似”与“原文相等”须分开。无需为内容可靠性引入Markdown AST后重新序列化，因为那可能连带改写未编辑内容；更适合AST只做范围定位，最终在原始文本上exact splice。

[字面$替换](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/editor.ts#L185-L188)；
[EOL策略](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/line-endings.ts#L20-L36)；
[patch载入归一](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.ts#L195-L228)；
[输出恢复EOL](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.ts#L247-L287)。

## 6. 原测试证据与真实离线复现

### 已读原测试，未运行完整Vitest suite

- editor.test.ts：单行/多行 diff最小区域、行数变化、diff截断双方可见、CRLF插入与替换、纯LF、字面$、insert_line=0拒绝。
  [diff测试](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/editor.test.ts#L103-L190)；
  [EOL和字面$测试](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/editor.test.ts#L192-L282)。
- apply-patch.test.ts：canonical grammar、旧shell wrapper、marker字面内容、sentinel、CRLF更新/移动、ADD现有文件拒绝、hunk失配不写盘。
  [格式兼容](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.test.ts#L18-L132)；
  [CRLF](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.test.ts#L159-L234)；
  [拒绝与不写盘](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/apply-patch.test.ts#L236-L298)。
- definitions.test.ts：编辑工具schema/输入传递/大输入拒绝。
  [工具包装测试](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/definitions.test.ts#L1894-L2107)。

### 已实际执行：18项函数演示，18/18符合观察预期

没有安装Cline、Bun依赖、Vitest或模型SDK；直接加载固定提交原始TS文件，由Node24内置 stripTypeScriptTypes 做语法转换、loader补相对import的.ts扩展名。未更改任何匹配算法。执行时用 env -i 清空环境，仅保留PATH；fixtures写入研究目录。代码运行只导入 node:fs/path/os 等内置模块和4个原始executor/helper文件，无网络或模型调用。

这18项是**自编断言对原函数的离线演示**，不是“上游测试全部通过”。其中PASS包括成功复现不适合精确编辑的危险行为，不能当作安全认证。

| 演示 | 实际结果 |
|---|---|
| editor中文跨段、CRLF、未动数学/表格、$与反斜杠 | 只改指定片段，字面内容与外部区域保留 |
| editor两个非重叠重复 | 拒绝；原文不动 |
| editor重叠重复：哈哈哈/哈哈 | 意外接受第一处，变呵哈 |
| editor空白不符、Unicode组合不符 | 拒绝 |
| editor包含旧文的替换重放、insert重放 | 都重复添加 |
| patch中文相似度0.8候选先于0.9候选 | 接受0.8第一处，fuzz1000 |
| patch后方exact与前方fuzzy | 优先后方exact |
| patch数学减号−与ASCII-不相等 | 接受且fuzz0 |
| patch首尾trim | 接受，fuzz100 |
| patch不存在@@锚点 | 仍修改第一处匹配内容 |
| parser一成功一失配 | parser留一chunk+warning；executor整体拒绝、不写盘 |
| patch后hunk上下文与前hunk重叠 | 拒绝、不写盘 |
| patch跨段+均一CRLF | 保留CRLF |
| patch混合EOL | 未编辑行也改为CRLF |
| patch纯插入重放 | 重复插入 |
| patch第二文件路径制造I/O失败 | 第一文件已改，无回滚 |

执行命令与完整日志：`demo/commands.txt`、`demo/execution.log`。机器可读结果：`demo/results.json`。可重跑脚本：`demo/run.mjs`、`demo/ts-loader.mjs`。未做性能压力测试、故障崩溃注入、整个UI/权限或并发调度验证。

## 7. 可复用组件、适配代价、许可证

### 可复用的小组件

1. editor替换的“小片段原文+新文”协议、零命中/多命中错误形式、literal replacement回调。执行器约268行，关键replace逻辑约30行。
2. createLineDiff 的共同前缀/后缀裁剪与双向预算，适合局部审核预览；不要让截断预览代替完整变更审计。
3. line-endings.ts 的检测思路适合均一EOL；OptoMind若要求精确保留混合EOL，不直接套用全文件normalize。
4. apply_patch 的grammar/AST可作“外部补丁输入适配器”，以及computePatchChanges的“先计算预览、再写入”分层。**不复用canonicalize/trim/fuzzy自动接受策略**。
5. 原测试fixture中的CRLF、美元符号、wrapper token cases可转换为OptoMind回归用例。

### 必须新增的可靠性层

- 将任意磁盘path改为受控doc_id与服务端路径映射。原 executor 的restrictToCwd对绝对路径直接放行，不能当安全沙箱。
- 接受 base_version或base_sha256、operation_id与每块预期原文。先检查文档快照，再解析所有候选。
- 严格match_mode默认仅literal exact；错误时返回候选建议，但禁止相似度直接提交。即使保留fuzz字段，也不能把fuzz0当exact凭证。
- 独立的section_id/已校验offset可限制候选范围；指定anchor找不到必须失败。重复候选要显式报歧义，而非取第一处。
- 批内所有edit都在同一快照解析为原始范围；检测重叠/冲突后自后向前splice；全部成功才以新version提交一次。
- 服务端CAS/事务提交、持久op_id收据；超时后查询收据，勿盲重放。
- 逐块返回 `matched_count/start/end/before_hash/after_hash/status` 等结构化证据，精确保留未编辑区域字节；支持审计和逆操作。
- editor的6000长度限制可作为可配置guardrail，不必照搬；先避免长hunk的Levenshtein二次矩阵扫描。

这属于“小型文本算法可少量借鉴，中等规模可靠性封装需自建”。照搬整个@cline/core更重，且仍不能提供OptoMind需要的文档事务语义。若要把apply_patch改到相同强保证，需要替换定位核心、添加歧义处理和事务封装，适配代价明显高于从editor式exact replace起步。

[路径策略](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/editor.ts#L42-L64)；
[公共导出的小执行器](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/src/extensions/tools/executors/index.ts#L26-L41)。

### LICENSE与依赖核对

- 仓库根LICENSE为Apache-2.0；已保存原文。复用/分发应带许可证，保留适用notice，修改文件显著标注；是否另需NOTICE以实际分发件为准。本次固定tree未见NOTICE文件。
- 4个执行源码的运行时依赖仅Node内置模块和彼此；`@cline/shared`、schema/types导入在这些executor中为type-only，可在抽取时替换成本地类型。parser无第三方runtime import。最小演示因此无需安装依赖。
- 若完整引入tool wrapper/schema，则额外涉及zod、@cline/shared及更多工具定义依赖。直接安装@cline/core 0.0.92还拉入@cline/agents/@cline/llms等workspace包、MCP SDK、OpenTelemetry、ws、yaml等；并非轻量文本patch库。
- 本次只核对清单中的依赖边界与根许可证，没有下载完整依赖树作逐包许可证/漏洞审计。不要把根Apache-2.0外推为所有依赖同许可。

[Apache-2.0](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/LICENSE#L1-L5)；
[再分发条款](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/LICENSE#L89-L128)；
[core清单和运行时依赖](https://github.com/cline/cline/blob/8eba11b2703f5b12d3f415544be37ffc0ca03be1/sdk/packages/core/package.json#L1-L106)。

## 8. 本地交付清单

- `report.md`：本报告。
- `repo.json/branch.json/tree.json`：锁定版本的API证据。
- `source/`：24份固定提交源码、原测试、许可证和清单；未修改。
- `source-verification.json`：Git blob SHA逐文件验证。
- `demo/`：完全离线的18项函数演示、执行日志、命令、JSON结果、保留的fixture结果。

