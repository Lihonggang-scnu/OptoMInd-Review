# Codex apply_patch 源码审计：OptoMind 科学 Markdown 局部编辑

## 结论

**可以借用协议和测试思路，不适合把当前执行器原样作为“精确科学编辑”内核。** 它是面向模型生成补丁的、按行定位且有宽松回退的执行器：不要求匹配唯一，容忍缩进与 Unicode 符号差异，不提供 exactly-once、文件版本 CAS 或多文件事务。当前版本的换行保真已经明显改善，不能沿用旧版本“全部转 LF”的判断；但尾换行和新增行的换行策略仍不是字节级精确替换。

## 固定版本与证据边界

- 仓库：https://github.com/openai/codex
- 默认分支：main，通过 git ls-remote --symref / origin HEAD 验证。
- 读取日期：2026-10-12 UTC；记录日志时间 2026-10-12T05:32:42Z。
- 固定 HEAD：[a9b0815167c27ab13719dd12d13b63e02b70d221](https://github.com/openai/codex/commit/a9b0815167c27ab13719dd12d13b63e02b70d221)，提交时间 2026-10-11T23:44:43Z。
- 当前源码在 upstream/，未修改。以下源码结论均按此 SHA；所有链接固定此 SHA。
- 尝试 cargo test -p codex-apply-patch --offline，未运行：环境无 cargo/rustc。没有安装 Rust、启动模型、调用付费平台或修改生产算法。
- 次级离线演示使用环境已安装的 codex-cli 0.159.2 专用 --codex-run-as-apply-patch 入口。二进制 SHA256 为 1748767b230ebfc3d4ab7e4e254920d0c0ad9691fd8c11f190e7d44511a4a92e，**未核实与上述源码 SHA 对应，不能称当前 HEAD 单测通过**。该演示只佐证所观察二进制，不替代固定源码证据。

## 1. 实际协议、提示、schema 和 parser

1. 当前 tool spec 是 FreeformTool，名称 apply_patch；format.type=grammar、syntax=lark；提示明说不要用 JSON 包裹。多环境模式会在 Begin Patch 后加入可选 Environment ID。
   - [工具定义](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/core/src/tools/handlers/apply_patch_spec.rs#L5-L27)
   - [实际 Lark 文件](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/core/assets/tools/apply_patch.lark#L1-L19)
2. 提示使用 Begin/End Patch，Add/Delete/Update File，Move to，@@ 和带 + / - / 空格前缀的行。@@ 后是单行文字定位上下文，不是 unified diff 的数字行号。仓库中不同模型提示并不完全相同；不能把某一个旧提示当成全部 Codex 的当前提示。
   - [GPT-5.2 通用提示的 apply_patch 段](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/core/gpt_5_2_prompt.md#L254-L288)
   - [Codex 专用提示只给较短使用建议](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/core/gpt-5.2-codex_prompt.md#L7-L13)
3. parse_patch 默认 Lenient：允许特定 EOF heredoc 包装，trim 整份 patch、按 lines 分割再以 LF 重组，交给 StreamingPatchParser。实际 parser 并不调用 Lark 引擎。官方 parser 顶部的 grammar 注释并非最精确版本：例如注释 add_line 用 (.+)，实际 tool grammar 用 (.*)，允许空新增行。
   - [lenient 开关](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/parser.rs#L47-L60)
   - [解析入口及边界容错](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/parser.rs#L145-L273)
4. 实际状态机把 + 行入 new_lines、- 行入 old_lines、空格 context 行入两边，并额外记录 context_line_indices；这允许替换时保留原 context 的字节与换行，而非用模型抄回来的 context 重写。
   - [数据类型](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/parser.rs#L114-L142)
   - [状态机行处理](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/streaming_parser.rs#L227-L367)
5. 这是文件级/整行协议；不理解 Markdown AST、数学公式、引用、表格、代码 fence 或科学单位。也没有 old_text/new_text 的原生 JSON schema。

## 2. 匹配策略：精确优先但不是严格匹配

[seek_sequence 完整定位逻辑](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/seek_sequence.rs#L12-L110)：

1. 从当前行游标开始，扫描全部候选，先找完全相等的连续行序列。
2. 完全找不到才重新扫描，用 trim_end 比较，忽略尾空白。
3. 再用 trim 比较，忽略两端空白，包括缩进。
4. 最后 trim 后逐字符归一化 Unicode：多种横线以及 **U+2212 数学减号** → ASCII '-'；弯单引号/双引号 → ASCII 引号；NBSP、窄不换行空格和若干排版空格 → 普通空格。
5. 每一档找到首个匹配就返回；**不检测零以外的匹配数量，不拒绝多个命中，不报告采用了哪一级回退**。因此后面的 exact 可以优先于前面的 fuzzy，不是“全局最早近似命中”。
6. 不是 Levenshtein/任意模糊匹配：中间普通空格的数量、字母大小写、一般文本内容仍需在相应归一化后相等。不要把它描述成任意近似替换。

科学编辑风险：ASCII '-' 与真正数学减号、普通空格与科学排版空格并非总是可互换；缩进也可能改变 Markdown 代码块/列表含义。当前 ApplyPatchOptions 只有 follow_symlinks，没有 strict/unique_match/normalization 开关：[选项定义](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/lib.rs#L59-L72)。

EOF 注意：seek 的顶部注释说末尾定位失败会从 start 回退，但**当前实现并未这样做**：search_start 是 eof_start.max(start)，各档只在这个范围扫描。应以 [L29-L36](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/seek_sequence.rs#L29-L36) 为准。old_lines 最后空字符串还可被单独去掉重试，[file_update L102-L122](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/file_update.rs#L102-L122)。

## 3. Snapshot、执行顺序、重叠、重复执行

### 同一个 Update File 的多个 chunks

- 文件只读成一次 original_contents / original_lines；所有 chunks 都对这个原始行集合定位，先收集 replacements，全部成功才重建结果。[读取→计算→重建](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/file_update.rs#L25-L54)
- 游标单调前进；@@ 文字先定位并把游标设为其后一行，chunk 匹配后游标设为 start + old_lines 长度。[计算过程](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/file_update.rs#L65-L165)
- 所以后一个 chunk 不能匹配前一个 chunk 刚生成的新文本；必须按原文位置顺序提供。相同原文区域的重叠（包括相交 context）通常因游标而找不到，报 expected lines，而不是专门返回结构化 overlap 错误。
- 原文为空的 chunk 是 EOF 追加，**即使有 @@ 文字定位，也不是在那个上下文后插入**；空 old_lines 直接放到 original_lines.len()。[L86-L89](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/file_update.rs#L86-L89)
- replacement 末尾按 source index 排序；SourceFile 假定 source-ordered non-overlapping，只有 debug_assert 守卫，并非独立的冲突合并器。[重建代码](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/text_file.rs#L79-L108)

### 整份 patch 和入口差异

- 独立 CLI / apply_patch()：parse 完整 patch 后，按文件 hunk 顺序逐个真实读写。重复 Update File 同一路径在此入口没有统一拒绝，后一个 hunk 能读到前一个写入，表现为顺序链式操作。[lib 应用循环](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/lib.rs#L469-L582)
- Codex 当前正式工具：先 verify_apply_patch_args 扫整份 patch，按 source path 放 HashMap，**拒绝同一 source path 多个操作**，错误 multiple operations target。[验证实现](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/invocation.rs#L180-L251)、[handler 先 verify 后 execute](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/core/src/tools/handlers/apply_patch.rs#L317-L369)
- verify 不是所有文件同时获取的不可变快照；runtime 又对原始 patch 调 apply_patch_with_options，再次读取文件并重新匹配。未看到 document version/hash CAS 或读取与落盘间的并发锁，因此不能声称审批时内容与落盘内容绝对同版本。[runtime L181-L199](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/core/src/tools/runtimes/apply_patch.rs#L181-L199)
- 没有 operation ID/幂等记录。重复替换可能因 old 不在而失败；原文有重复片段时第二次可能继续改下一个；纯添加再次运行会再追加。二次执行不是天然安全重试。

## 4. 原子性、部分成功、错误反馈

- 同一个 Update File 内的**定位失败**发生在写文件前，因此当前文件通常不变；不能据此称文件写入原子。
- 多文件按顺序写。后续文件失败，前面文件修改保留，无回滚。Add File 也可覆盖现有文件，Move to 可覆盖目标；这有现成单测，不是猜测。
  - [部分成功单测](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/tests/suite/tool.rs#L377-L395)
  - [覆盖测试](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/tests/suite/tool.rs#L284-L321)
- 当前源码显式承认 write 失败也可能已经 truncate/部分写入，并标记 delta.exact=false；local direct filesystem 用 tokio::fs::write，没有临时文件+原子 rename 的协议保证。
  - [try_write 注释和处理](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/lib.rs#L454-L466)
  - [local 写文件](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/exec-server/src/local_file_system.rs#L622-L635)
- Move 实现先写目的地、后删源；删除失败可留下两份文件。[移动失败已提交 delta 测试](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/lib.rs#L1071-L1123)
- crate 返回 AppliedPatchDelta / ApplyPatchFailure，能记录已提交部分、old/new 内容与 exact 状态，这是可借用的优秀设计；它记录事实而不是执行 rollback。[delta 结构](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/lib.rs#L213-L303)
- parser 错误带 hunk 行号/说明；定位失败带找不到的 context 或 expected lines；正常 stdout 只列 A/M/D 路径，不提供匹配次数、选中偏移、宽松程度或科学语义安全性。failure CLI 可 stdout 为空但前面已写成功，不能只看 stdout 判断“无副作用”。[parser errors](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/parser.rs#L55-L60)、[summary](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/lib.rs#L824-L840)

## 5. CRLF、上下文和字节保真

当前 SourceFile 不再把整个文件按 LF 重写：

- 按字节扫描 CRLF、独立 CR、LF，保留每行原 ending；第一个 existing ending 为新增行首选，无 existing ending 则 LF。[parse](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/text_file.rs#L30-L77)
- 未变行保留内容和 ending；明确 context 行也单独保留，所以模糊匹配不会自动把 context 的 Unicode/缩进抄坏。[context 保护](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/file_update.rs#L124-L152)
- 被替换/新加行统一用 preferred ending，而不是该位置原 ending；混合行尾文件的 changed line 可从 LF 变 CRLF。
- 为**所有结果行**补 ending，包括原来无终止符且此次未改的末行。因此“只改第一行”也会添加尾换行，不是纯目标字节变动。[apply_replacements](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/text_file.rs#L79-L119)
- 仅支持能解为 UTF-8 的文件文本；非法 UTF-8 拒绝，非任意二进制补丁。[read_file_text](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/file-system/src/lib.rs#L661-L671)
- [原有单测 L107-L181](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/tests/suite/tool.rs#L107-L181) 明确覆盖 CRLF、CR、重复 context、mixed endings、给原末行补 CRLF。静态核对一致，未执行该 Rust test target。

## 6. JSON old/new 能否适配

**能做协议适配，不能靠换 JSON 外壳获得严格语义。**

- 若片段以整行为边界：将 old 每行加 '-'、new 每行加 '+'，包进一个 Update File/@@；或直接构造公开的 Hunk::UpdateFile / UpdateFileChunk，调用公开 apply_hunks。它不是现成 old/new JSON API，需要自己的 serde/schema 层。
- 若片段是行内子串：必须根据同一文档快照扩展为完整行，再产生补丁；直接把子串放 old_lines 找不到“行中一部分”。同一行多个 edit 要先在适配层协调。
- old 为空不代表“给定 offset 插入”，执行器默认 EOF；要在文中插入必须提供前后 context。跨块 edit 必须先按原文位置排序、去重和拒绝重叠。
- 上游 derive_new_contents_from_chunks 是 pub(crate)，不直接导出纯 text→text API；公开 unified_diff 函数的结果字段同样是 pub(crate)。外部可走 verify_apply_patch_args→ApplyPatchAction::changes() 取得 proposed new_content，或维护薄 fork 导出纯函数，但这仍会保留宽松定位和尾换行语义。[函数/结果可见性](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/file_update.rs#L18-L31)、[结果类型](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/file_update.rs#L168-L204)、[changes getter](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/lib.rs#L181-L188)

OptoMind 所需的差异应显式设计为自己的 contract：文档版本/hash、预期 old 精确字节、唯一位置或稳定锚点、零/多匹配拒绝、对原快照批量算位点、重叠拒绝、内存中全量验证后提交、变更范围外字节保持、幂等 operation ID、结构化错误。是否进行 Markdown/LaTeX 语法校验属于其上层业务校验；通用 apply_patch 没有提供。

## 7. 许可证及抽取成本

- codex-apply-patch 继承 workspace 的 **Apache-2.0**；[workspace 元数据](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/Cargo.toml#L161-L168)、[LICENSE](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/LICENSE)、[NOTICE](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/NOTICE)。抽取/分发时要核对并保留适用许可证和 notices、标明修改；依赖各有许可证，不能把仓库 Apache 标签当成全部传递依赖的许可证清单。
- 当前 crate 并非零依赖的小函数包。直接 dependencies 共 9 项：anyhow、codex-exec-server、codex-utils-absolute-path、codex-utils-path-uri、similar、thiserror、tokio、tree-sitter、tree-sitter-bash；dev dependencies 5 项。[Cargo.toml](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/Cargo.toml#L19-L35)
- codex-exec-server 又依赖 axum、tokio、OpenTelemetry、HTTP/WebSocket、network proxy、sandboxing、protocol/config 等多个内部 crate；因此“只 cargo build -p codex-apply-patch”虽不启动模型，却不等于依赖体积很小。不能从本次未编译结果声称构建分钟数或二进制 MB。[exec-server manifest](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/exec-server/Cargo.toml#L21-L95)
- 固定工具链 Rust 1.95.0，edition 2024；[rust-toolchain](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/rust-toolchain.toml#L1-L3)。
- 最省依赖的抽法是 vendoring 匹配/文本重建纯逻辑（seek_sequence、SourceFile、compute_replacements）并重新定义边界 types/I/O；Tree-sitter 主要用于识别 shell heredoc invocation，不是补丁行匹配本身，可在纯 JSON/text executor 中移除。similar 只用于统一 diff 预览；tokio/exec-server 用于异步文件系统，内存 API 可替换。这个方案是**维护自己的改编分支/派生执行器**，不是直接调用上游稳定的纯文本库接口。
- Rust 抽取成本判断：比整个平台轻，但仍要设计公开 text→text API、错误码、JSON 反序列化、严格语义和提交边界。其他语言移植需重实现 Unicode trim、CR/LF 分行、上下文身份、游标/EOF 和部分成功语义，不能只抄一个 replace 函数就称等价；优先携带上游可移植 fixtures 作为兼容测试。

## 8. 实测与未测范围

### 固定 SHA：静态核对，未跑 Rust tests

- seek_sequence 内四个基础单测：[L122-L163](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/seek_sequence.rs#L122-L163)
- Unicode dash 单测：[lib.rs L1277-L1321](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/src/lib.rs#L1277-L1321)
- 重叠 EOF 拒绝且原文件不变：[tool.rs L87-L104](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/tests/suite/tool.rs#L87-L104)
- CRLF/CR/mixed 和部分成功测试见上。
- 原始 scenarios suite 只比最终目录/文件 bytes，故负例可非零退出且仍“测试相符”；它不检查退出状态：[scenarios.rs L30-L65](https://github.com/openai/codex/blob/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/apply-patch/tests/suite/scenarios.rs#L30-L65)。

### 次级附录：现成 0.159.2 二进制（与源码 commit 未绑定）

- 固定 SHA 的 25 个原始 scenario fixture 数据原样重放，最终目录/文件字节快照 25/25 相符。**不等于执行 25 个 Rust 单测或证明 HEAD 构建通过。**
- 自定义黑盒 10 例：
  1. 重复 x=1 第一次只改首个，第二次改第二个，两次都 exit 0。
  2. old 用 ASCII '-' 命中实际 U+2212 减号，exit 0。
  3. 前面缩进不同、后面 exact 相同，修改后面 exact。
  4. 同一 Update File 的 a→b 再 b→c 失败，a 文件不变，证明 chunks 非链式。
  5. 独立 CLI 重复两个 Update File 对同路径 a→b→c 成功；这个结果不能用于正式 tool 的 verify 路线。
  6. 先 Add created、再 Update missing，exit 1 但 created 保留。
  7. CRLF input 的新增/替换继续 CRLF。
  8. fuzzy context 的原缩进/CRLF 保留，changed line 使用 preferred CRLF。
  9. 只改首行却给原来未终止的末行补 CRLF。
  10. 两个重叠 chunk 失败，原文件 one 不变。

本次没有测：编译耗时/体积、完整上游 CI、当前 SHA 编译出的代码、模型生成成功率、并发竞争/崩溃注入、各平台文件系统/远程 fs、安全沙箱完整行为。它是协议和执行语义审计，不是模型效果 benchmark。

## 本地材料

- upstream/：固定 SHA 的 sparse checkout、上游 LICENSE/NOTICE、相关源码/原始 fixtures。
- logs/source_and_build.txt：版本、日期、cargo 缺失的真实输出。
- demos/run_installed_binary.py 与 demos/installed_binary_results.json：10 个黑盒案例，原始输入/patch/stdout/stderr/结果。
- demos/run_upstream_fixtures_installed_binary.py 与 demos/upstream_fixture_results.json：25 个原始 fixtures 重放与逐项 bytes。
- logs/installed_binary_demo.txt、logs/upstream_fixture_demo.txt：命令输出。
