# OpenHands/software-agent-sdk：局部文本编辑核实报告

## 结论先行

适合借鉴的是“编辑前获取实际文本、唯一匹配失败时明确反馈、成功后给局部片段与旧/新内容、单文件原子写入”这些闭环设计，不宜把默认 FileEditor 原样当成严格文稿补丁引擎。它有隐藏的 old_str.strip() 退路、非重叠计数造成的唯一性空洞、CRLF 转 LF、无操作幂等 ID、非事务化历史记录。若 OptoMind 现有 contracts 已能严格定位，优先复用现有严格定位，再吸收这些反馈/提交设计，比引入整个 SDK 更小、更可控。这是本组件分析，不预设跨项目最终赢家。

SDK 有三个不同编辑执行器；不能合称为一个“OpenHands 编辑算法”：

- 默认 preset：FileEditorTool，JSON command/path/old_str/new_str 单次编辑。[默认 preset](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/preset/default.py#L36-L61)
- GPT-5 可选 preset：ApplyPatchTool，OpenAI cookbook 风格文本补丁。[GPT-5 preset](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/preset/gpt5.py#L1-L5)
- Gemini 可选 preset：EditTool，old_string/new_string/expected_replacements；另配 write_file。[Gemini preset](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/preset/gemini.py#L42-L74)

## 1. 基线与许可证

- 仓库：https://github.com/OpenHands/software-agent-sdk
- 读取时间：2026-10-12 05:29–05:35 UTC。
- git ls-remote --symref 核实默认分支 main；克隆并固定 SHA：e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7。
- 该提交时间：2026-10-12 05:23:46 UTC；标题 fix(llm): keep provider credentials out of completion logs (#5657)。源码目录 git status 干净。
- 仓库 MIT，Copyright 2026 OpenHands contributors；复制或实质抽取需保留版权与许可声明。[LICENSE](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/LICENSE#L1-L21)
- apply_patch/core.py 明确声明改编 OpenAI cookbook 的 apply_patch.py。[来源说明](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/apply_patch/core.py#L1-L17)

## 2. 默认编辑器：真实协议与执行链

### Schema 和提示

FileEditorAction 的 command 为 view/create/str_replace/insert/undo_edit；path 要求绝对路径；编辑通过 old_str 与 new_str 字段。不是 SEARCH/REPLACE 标记文本，也没有 patch parser、JSON Patch 路径、block ID 或文档版本哈希。[Schema](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/definition.py#L26-L66)

提示要求先 view、old_str 包含足够上下文（建议 3–5 行）、精确且唯一、new_str 与 old_str 不同；建议同一消息发多次工具调用。提示说“完整连续行”，但实现允许任意行内子串。提示说精确，但实现有 strip 退路。[提示](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/definition.py#L160-L190)

另有接口不一致：schema 描述说 str_replace 的 new_str 可省略，运行时 __call__ 明确拒绝 None。删除要传空字符串 ""，不能省略。[schema L47–51](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/definition.py#L47-L51)，[运行时 L134–146](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L134-L146)。

### 定位、替换、错误、回读

FileEditorExecutor → FileEditor.__call__ → str_replace：

1. 校验文件并读取全文。
2. re.escape(old_str)，re.finditer 收集字面、非重叠命中；记录起始行号、原文、字符索引。
3. 若零命中，只对 old_str.strip() 后重试；new_str 不裁剪。
4. 仍零命中：ToolError，带找不到的原文；多于一处：ToolError，带重复位置行号。均不写文件。
5. 一处：用切片构造完整新字符串；write_file 写入；再保存历史；再读局部片段，输出成功消息、old_content/new_content。

[完整匹配与替换](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L177-L281)。Executor 捕获 ToolError 包装 is_error=True 文本观察；没有专门的 NOT_FOUND / AMBIGUOUS / STALE_VERSION 枚举或失败候选片段。[错误返回](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/impl.py#L57-L73)

成功输出提醒检查效果、必要时继续编辑；代码没有自动重试模型、自动扩上下文或自动全文重写。GUI 的 _diff_cache 只缓存可视化 diff，并不是匹配缓存。[观察对象](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/definition.py#L69-L148)

## 3. 默认编辑器边界

### 唯一性、重复、跨段

- 全文件域唯一；支持单行、行内片段、含空行的跨段连续原文、数学符号和 Markdown 表格字符串，不了解段落或表格结构。
- 不支持不连续的多选区域或结构化表格单元格编辑；一个 str_replace 只有一个 old/new。
- 普通重复单行和重复多行块拒绝；错误列出起始行号。
- 唯一性是“非重叠 regex 命中数为一”。实测 'aaa' 中 old='aa'、new='X' 成功为 'Xa'，没有识别位置 1 的重叠第二命中。
- 空 old_str 在非空文件出现多处，被拒绝；在空文件只有一个零宽匹配，实测能插入。接口没有统一拒绝空搜索串。

[实现证据](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L198-L248)，[上游重复/空串测试](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/tests/tools/file_editor/test_basic_operations.py#L481-L554)。

### 空白、Unicode、CRLF、数学/表格

- 精确命中优先；零命中时 strip 去掉搜索串首尾所有 Python 定义的空白，可能删掉首尾空行/制表符/Unicode 空白；内部空白不放宽。
- new_str 首尾空白保留，因此 Markdown 两个尾随空格、缩进不会在 fallback 中被额外剥掉；上游已有回归测试。[空白测试](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/tests/tools/file_editor/test_file_editor_tool.py#L342-L405)
- 不做 NFC/NFD、全半角、数学排版或语义规范化。实测 café 与 cafe+组合重音不匹配。正则元字符先 escape，LaTeX/数学美元符号不会变成 regex。
- read_file 用普通文本 open，Python universal-newline 把 CRLF 读成 LF；写回新临时文本文件。Linux 实测只改一处也把所有未改行 CRLF 变为 LF。含内部 CRLF 的 old_str 不会对应规范化，会找不到。[读取](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L744-L786)，[写入](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L480-L529)
- 编码层优先 UTF-8，支持 BOM 与检测/恢复；无法以旧编码表示新字符时全文转 UTF-8。这是明确存在的整文件编码变动，不是模型全文生成。[编码](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/utils/encoding.py#L61-L186)，[原子写与 UTF-8 退路](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L480-L503)
- 上游明确保护 U+2028、form feed 等不被 splitlines 当额外行拆开，但这不是 Unicode 规范化。[行展示修正](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L788-L846)

### 顺序、重叠编辑、部分成功、重放

- 一个编辑调用内部只有一次替换。多调用默认按顺序执行，后一项读前一项修改后的文件；原始同一基线上的多个重叠 edit 没有统一计划或冲突检测。
- Agent 默认 tool_concurrency_limit=1；设为更大时按规范化文件路径上资源锁（连 view 也锁），不同文件可并行。并发锁不等同于文档版本比较、批次事务或跨工具/进程写入保护。[并发默认值](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-sdk/openhands/sdk/agent/base.py#L314-L322)，[编辑器声明资源](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/definition.py#L198-L208)，[执行器](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-sdk/openhands/sdk/agent/parallel_executor.py#L98-L126)
- 第二条编辑失败不会回滚第一条；实测可再显式 undo 恢复第一条。
- 不幂等。old 消失时重放会报 not found；若 new 仍唯一包含 old，重放再次生效：x→x! 连续两次得 x!!。工具本身声明 idempotentHint=False。[注解](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/definition.py#L261-L266)
- 没有 read-version/hash CAS，未读最新内容只要 old 仍唯一存在即可成功。直接使用 FileEditor 类不会自动得到 agent 层资源锁。

## 4. 写入、历史、缓存和全文重写边界

- 每次成功局部替换都构建完整新文件，并通过同目录临时文件 + Path.replace 全文件原子替换，保留 mode bits。不是字节原位 patch；没有 LLM 全文重写 fallback。
- 原子 replace 前失败可保留原文件、移除临时文件。上游测试和本次失败注入都覆盖该情况。但没有 fsync/目录 fsync，因此不要将它升级描述为断电持久性事务。
- 历史保存于临时目录 JSON 文件；FileCache 用 key 的 SHA256 做文件名；默认编辑器每文件保留 10 个文本快照。FileHistoryManager 单独默认 5 个；FileCache 构造未传全局 size_limit，不能据 docstring 说有默认总磁盘上限。[初始化](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L88-L94)，[历史](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/utils/history.py#L17-L89)，[缓存](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/utils/file_cache.py#L14-L89)
- 这是 undo 历史，不是可用于匹配的读缓存或索引；每次替换读实际文件全文。编码另有 1000 项 LRU，以路径+mtime 判断缓存有效。[编码缓存](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/utils/encoding.py#L41-L59)
- 并非严格事务：先 write，再 add_history。历史磁盘失败时内容已变；本次失败注入验证。undo 先 pop 并删快照，再 write，若 undo 写失败会失去该快照；本次也验证。[提交顺序](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L251-L262)，[undo 顺序](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/editor.py#L671-L692)
- 快照保存解码后的文本，不能保证回滚到原 CRLF/编码/全部文件元数据。新 FileEditor 实例默认新建历史目录；不要将提示“跨讨论持久”解读成进程重启后自动恢复。
- 默认编辑器 workspace_root 主要用于路径提示；不是文件访问边界。Executor 的可选 allowed_edits_files 能限制写，但默认 None；独立抽取要自带安全路径约束。[访问限制](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/file_editor/impl.py#L25-L55)

## 5. 两种可选 preset：只做边界说明

### ApplyPatchTool

- 语法为 *** Begin Patch / *** Update File 等，不是标准带行号 unified diff。[schema/executor](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/apply_patch/definition.py#L29-L122)
- 匹配依次 exact → 每行 rstrip（fuzz=1）→ 每行 strip（fuzz=100）；每阶段选第一处，无唯一性检查。较晚的 exact 胜过较早的 fuzzy。EOF 失败可退回普通搜索并加 10000。[核心匹配](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/apply_patch/core.py#L231-L262)
- 多 hunk 基于原始全文、向前 cursor 定位；重复 path 拒绝；回退/重叠上下文通常失败；不是每 hunk 在前一个修改结果上重新解释。[解析](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/apply_patch/core.py#L116-L213)，[重建](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/apply_patch/core.py#L369-L434)
- 所有解析、生成 commit 完成后再逐文件写，因此后面 hunk 匹配失败时零写；但 I/O commit 非事务，第二文件写失败会保留第一文件变动。Executor 用直接 open('w')，没有默认编辑器的临时文件/历史。[process/apply](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/apply_patch/core.py#L461-L502)
- Add File 并不检查已存在路径：实测可覆盖。无 undo、operation ID 或重放保护。路径限制与默认不同：executor enforce resolved path 位于 workspace；其实现允许 workspace 内的绝对路径，注释“拒绝绝对路径”比实际更严格。
- 适合独立抽取的是 core.py：502 行，stdlib + Pydantic，I/O 通过函数注入，无 SDK/LLM 依赖。不过严格局部文稿引擎不能直接继承它的 first-match/strip 语义。

### Gemini EditTool

old_string/new_string/expected_replacements（默认 1）。str.count + str.replace，计数必须等于期望值；没有 strip fallback，支持指定替换所有 N 个非重叠重复。empty old 表示建新文件；同 old/new 返回错误。UTF-8 errors='replace' 读取会把非 UTF-8 字节替为 U+FFFD，写回直接 open('w')；没有原子写/历史。描述建议大修改考虑 write_file，是独立工具选择建议，不是程序自动失败退路。[schema/提示](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/gemini/edit/definition.py#L23-L93)，[实现](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/openhands/tools/gemini/edit/impl.py#L103-L182)

## 6. 抽取成本与 OptoMind 可借鉴点

- 只要默认字面匹配：算法本身仅需 stdlib re，但上游没有独立纯 match/plan API，嵌在 FileEditor.str_replace。应明确抽出自己的纯 plan 阶段并保留来源，不要为了几十行定位逻辑安装整个 agent SDK。
- 保留默认文件编辑模块：editor.py、exceptions.py、encoding.py、history.py、file_cache.py、config/constants；外部小依赖有 binaryornot、charset_normalizer、cachetools；SDK observation/action/logger/path/truncate、rich 可视化与 Pydantic 是额外耦合。若维持公开 SDK import，则会带整个 SDK 依赖，不是上述小依赖之和。
- 完整 openhands-tools 依赖 SDK、browser-use、tree-sitter、libtmux、tom-swe 等；SDK 再带 litellm、fastmcp、fakeredis、httpx、lmnr 等。均 Python >=3.12，当前项目声明 Pydantic >=2.13.5。[tools 依赖](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-tools/pyproject.toml#L1-L20)，[SDK 依赖](https://github.com/OpenHands/software-agent-sdk/blob/e3a26821f8f7c85e98078c7b5e1a80647fc7d0c7/openhands-sdk/pyproject.toml#L1-L31)
- 小成本借鉴：缺失/多命中的可操作反馈；old/new 结构化观察；成功后局部片段；严格先计划后提交；同文件串行；用同目录原子 replace 提交。
- 需要在 OptoMind 自身保证：严格定位无隐藏 trim/fuzzy、读版本/哈希、非空搜索、真正重叠歧义检查、幂等 op_id、批次全部校验与冲突检查、写前 journal/写后安全完成、原始换行/BOM/编码策略，以及公式/表格结构回读验证。以上不是 OpenHands 已实现能力。

## 7. 实际验证与复现范围

在修改任何演示前先读相关源码、测试体和导入：选定代码只做内存/本地临时文件操作；没有 LLM 创建/run、远程 workspace、浏览器、安装脚本、API key 读取。本次没有安装任何依赖、没有调用模型，没有改 OptoMind 或真实稿件。源码拉取属于前置公开研究，运行阶段断开网络能力。

环境 Python 3.12.14，已安装 Pydantic 2.13.4。由于低于完整 SDK 声明下限，且 SDK、pytest、rich、binaryornot、cachetools 均未安装，没有声称完整 SDK/pytest suite 通过。

实际运行：

```
env -i PATH="$PATH" HOME="$PWD/clean_home" LANG=C.UTF-8 python offline_probe.py
```

演示程序再使用 Python audit hook 拒绝 socket 与 subprocess/system。不要运行 make build、uv sync、examples 或 integration tests；这些不在离线演示范围。

结果：33 个上游原测试函数体 + 28 个原方法演示，61 项断言全部通过，0 模型调用。运行约 0.3 秒。这是一个固定的小型边界矩阵，不是质量 benchmark。

具体隔离：

- apply_patch/core.py 原文件完整 import，未改核心。定义文件中的原 executor/schema 节点通过 AST 载入；SDK Observation/ToolExecutor 基类换轻量壳。
- 默认 FileEditor 原类与原方法/with_encoding decorator 从源码 AST 载入；替换的仅是输出壳、固定 UTF-8 encoding manager、已知文本夹具上的 binary detector。原始匹配、切片、I/O、原子替换、历史、错误类均照用。没有重写匹配器；但不验证真实自动编码检测、二进制识别、SDK registry、LLM schema 序列化或并发调度。
- 运行 4 个默认替换原测试体、5 个 history 原测试体、2 个强制编码写入参数案例、13 个 apply_patch executor 原测试体、9 个 Gemini 原测试体。pytest fixtures/SDK 壳没有运行；原测试函数体未改。测试函数的装饰器按需移除以手工传夹具。
- 额外演示覆盖默认 exact/inline、跨段/重复、数学/表格、CRLF、Unicode、制表符、trim、空串、重叠、重复执行、部分成功+undo、atomic fail、history fail、undo fail；patch first-match/fuzz、解析零写和 I/O 部分成功。

可复核文件：offline_probe.py、offline_run.log、offline_results.json；最小打包清单见 demo_manifest.json，保留精确原文件和 LICENSE，不需要整份 SDK、更不需要真实文稿。
