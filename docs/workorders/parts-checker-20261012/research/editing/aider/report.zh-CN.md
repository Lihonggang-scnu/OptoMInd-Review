# Aider：科学中文 Markdown 局部编辑执行器审计

## 结论

**可借鉴 Aider 的 SEARCH/REPLACE 输出协议、简短示例及错误修复反馈；不宜把当前执行器原样用于 OptoMind 的自动科学文本修改。** 主要问题不是“正文会用编辑距离随便模糊替换”，而是：不强制唯一命中、缩进容错、非连续省略号替换、跨文件兜底、非事务式顺序写入和不幂等重放。`udiff` 的风险更大：长重复文本可被一次替换多处。

这是执行安全性的源码判断，不是对模型生成质量的测评，也不推荐引入完整 Aider 平台。

## 版本与证据边界

- 官方仓库：`Aider-AI/aider`；官方 GitHub API 返回默认分支 `main`。
- 读取日期：2026-10-12 UTC。
- 锁定 SHA：`5dc9490bb35f9729ef2c95d00a19ccd30c26339c`；该提交日期为 2026-05-22T14:02:20Z。[固定提交][commit]
- 本报告所有源码链接固定到该 SHA。保存了 29 个相关原始文件，逐个用 Git blob SHA 对照此提交的 Git tree 验证；并非完整仓库克隆。完整性记录见 `source_manifest.json`、`source_integrity.json`。
- 用户给出的[编辑格式文档][docs]可作为协议介绍；当前源码的注册表还包含 `patch`、`udiff-simple`、`editor-diff-fenced` 等，不能把网页列举或旧博客当作完整现状。[注册表][registry]

## 当前真正的调用链

`EditBlockCoder(edit_format="diff")` → `get_edits()` → `find_original_update_blocks()` → `apply_updates()` 的 dry-run / 权限准备 → `apply_edits()` → `do_replace()` → `replace_most_similar_chunk()`。

`diff-fenced`、`editor-diff` 都继承同一 `EditBlockCoder` 执行器，主要换提示。[diff 类与调用入口][eb-entry] [父级应用链][apply-updates] [diff-fenced][fenced] [editor-diff][editor]

提示要求完整路径、连续 SEARCH 行、逐字符一致和只改首次命中；让模型提供足够上下文使其唯一。但是“模型应该生成唯一上下文”不等于执行器验证唯一。[实际提示][prompt]

协议解析器接受 5–9 个分隔符字符、可以复用上一个文件名，甚至把 `=======` 当作 REPLACE 结束符；没有独立的字面量转义层。它也识别 shell 代码块，交给上层另作处理。提取用于文稿时应把 shell 路径移除，勿照搬 agent 级解析行为。[标记][markers] [解析器][parser]

## 八项真正影响选型的结果

### 1. `diff` 的正文 fuzzy 分支实际关闭，但 whitespace 容错仍开启

执行顺序是完整逐行相等 → 统一前导空白容错 → 尝试丢弃 SEARCH 的一个首空行 → `...` 分段处理。编辑距离 fuzzy 调用位于无条件 `return` 后，当前不可达。上游两项 fuzzy 测试也改名为 `__test_...`，不会被正常 unittest 收集。[定位链][match-chain] [空白匹配][whitespace] [关闭的测试][disabled-tests]

实测：`测量值为1.0。` 与 `测量值为1.0.` 不匹配；NFC `café` 与 NFD `café` 不匹配。另一方面，不带缩进的 SEARCH 可以改动原稿中四空格缩进的内容。对于 Markdown，缩进、空行可能决定代码块或列表结构，不能视为无害排版噪声。

### 2. 普通重复匹配仅改第一处，不验证唯一；重放会改下一处

`perfect_replace()` 从文件开头扫描，命中即返回；whitespace 路径也首次返回。上游测试明确验证“only replace first occurrence”。[exact 实现][perfect] [重复测试][repeat-tests]

实测：两段同为“结论。”时，一次只改第一段；把完全相同的 edit 再发一次会改第二段。同一响应含两个相同块，也会依次改两段。协议没有 `expected_count=1`、段落 ID、旧版本摘要或 edit ID，因此“只改一处”不是“精确改指定一处”，更不是幂等。

### 3. 目标文件和块边界都有超出字面协议的容错

文件名会先匹配完整路径、再匹配 basename、再用 `difflib.get_close_matches(cutoff=0.8)`。实际应用中，指定文件未匹配时会尝试聊天里的其他文件。[文件名解析][filename] [跨文件兜底][eb-entry]

实测：给 `doc.md` 的编辑在它无匹配时成功写入 `other.md`。这在编程交互中可能是纠错便利，在科学文稿中可能把相似实验结果写错文件。应将目标路径作为宿主系统绑定的精确值，禁止模型或执行器猜测。

此外 `strip_quoted_wrapping()` 会去掉其认为多余的文件名和围栏。实测：SEARCH 是完整 Python Markdown 围栏、REPLACE 改成 JavaScript 围栏时，只改内部文本，原来的 `python` 围栏还在。[包装剥离][wrapping] 这说明该函数默认把一部分文稿本体当作传输包装；不宜直接用于 Markdown。

### 4. 跨段可以，跨非连续区域也可以；没有段落语义

完整多行 SEARCH 可以包括空行，所以跨自然段替换正常成功。它按完整行匹配，不支持任意行内子串；单行很长的中文段落即使只改一句，通常也需要返回整行。

启用的 `try_dotdotdots()` 还能把成对的 `...` 行当作通配间隔，分别替换多个非连续片段。各非空片段要求在当时文本中恰好出现一次；但这不是普通 exact 路径的全局唯一性保证，也没有段落、公式、引文保护。[省略号实现][dots]

实测：`甲。\n...\n乙。` 可越过两个保留段，改动两个远离的区域；某个片段重复则失败。科学编辑建议拒绝省略号通配，强制每个操作是一个明确连续范围。

### 5. 多块顺序应用，部分失败不回滚；dry-run 也不是事务预演

`apply_edits()` 按 edit 顺序读当时文件，成功就立即写盘。后一个块可以匹配前一个块产生的新文本；若后一个仍搜索已消失的旧片段，会失败，但前一个改动已留下。错误消息明确说其他块已成功、不要重发。[顺序写入和错误反馈][eb-entry]

实测两种重叠情况均复现。不存在显式重叠检测、原稿 snapshot 定位或批次原子提交。`apply_edits_dry_run` 每块都读取磁盘但不把候选结果累计起来，不能据此声称“整批已在同一候选文档验证”。[父级应用链][apply-updates]

空 SEARCH 表示追加，重放会重复追加；已应用 edit 若再无 SEARCH 命中，会报错并附“REPLACE 已存在”提示，而不是可靠的幂等成功。若旧内容仍在别处，则可能继续改下一处。另一个边界：删光文件产生空字符串时，真值检查把它视作失败。实测都已记录。[创建/追加][do-replace] [已应用提示][eb-entry]

### 6. 错误修复机制值得借鉴，但必须分离“建议”和“自动应用”

无法匹配时返回失败 SEARCH/REPLACE、相似的实际行、已存在 REPLACE 的提醒，以及部分成功提示。`find_similar_lines(threshold=0.6)` 用于提示，不是正文自动替换。父级把 `ValueError` 放到 `reflected_message` 交给模型修复；默认最多 3 次 reflection。[诊断建议][similar] [错误接入][apply-updates] [循环][reflection] [默认次数][reflection-limit]

OptoMind 可采用这种反馈结构：明确 `NO_MATCH` / `AMBIGUOUS` / `STALE_VERSION`，给出候选上下文让模型重发。但是新尝试必须重新精确验证，不能因“相似”直接写入。本次只离线检查执行与反馈生成，没有付费模型，也没有评估自动修复成功率。

### 7. Unicode 内容大体按字面处理，但不是字节级无损协议

没有 Unicode normalization 或中文/英文标点折叠，所以内容差异通常拒绝。这有利于保留科学数值、符号的严格性，但不能推出整体字节无损。

`prep()` 可补末尾换行；`strip_quoted_wrapping()` 用 `splitlines()` 再以 LF 拼接 SEARCH / REPLACE。直接向匹配函数传 CRLF 文稿却传 LF SEARCH 会失败，`do_replace()` 甚至会先把 CRLF SEARCH 变成 LF。[预处理][match-chain] [包装/换行][wrapping]

完整 Aider I/O 则用 Python 文本读取默认 universal-newline，写入使用配置的 `platform` / `lf` / `crlf`，并非自动保留原文件每个换行字节。默认 `platform`。[I/O 默认][io-default] [I/O 读写][io] 保持未修改区域字节一致、BOM/换行/末尾换行策略，应由 OptoMind 的文件层明确处理。本次 CRLF 展示区分了直接原函数与本地文本 I/O 适配，不冒充完整 Aider I/O 集成测试。

### 8. `udiff` 不是更严格的替代品；当前另有 `patch`，也有宽松匹配

`udiff` 的 `directly_apply_hunk()` 仅在短于 10 个非空白字符且重复时直接拒绝；更长内容进入 `flexi_just_search_and_replace()`，后者当前只接 `search_and_replace` 策略。该函数的“多次出现则抛异常”代码被注释，实际 `str.replace(search, replace)` 没有限制次数。[udiff 调用][udiff-direct] [replace-all][sr] [预处理策略][sr-pre]

实测：两行相同的长中文科学结论，一次 hunk 改了两行。不能因为代码里存在 `UnifiedDiffNotUnique` 文案就宣称会强制唯一命中；也不能把 `search_replace.py` 的策略表误认成 `diff` 当前正在使用的执行器。

当前注册的 `patch` 类来自另一个 apply-patch 风格实现，包含 exact → rstrip → strip 匹配，应用前再核对时也 strip 两侧空白；它会检查重叠 chunk，但仍不是严格科学文本执行器。[patch 定位][patch-match] [patch 应用][patch-apply] 此分支仅静态核对，没有在本轮追加实验。

## 可抽取的小组件、依赖与接入成本

1. **最适合参考：提示规则、少量例子和结构化失败反馈的设计。** `editblock_prompts.py` 约 170 行，其中协议规则集中在 120–158；不要带入软件开发、shell、自动新建文件等 agent 行为。
2. **最小机械组件：`perfect_replace()` 9 行与相邻文本拆分逻辑。** 本身只需 Python 标准库，但它不查唯一性，还会返回字符串而非精确 span。若采用，必须在外层加唯一候选验证、版本检查与失败封闭策略，不能再称为“直接使用上游执行器”。
3. **可用作失败建议：`find_similar_lines()`。** 仅 `difflib`，27 行。只展示建议，不自动选择/应用候选。
4. **解析器可参考，不推荐原样摘取：** `find_original_update_blocks()` 97 行，加文件名/标记处理后约两百行；依赖 `re`、`pathlib`、`difflib`。它混合 shell、宽松结束符和文件名推断，对固定单文稿的协议来说过多。
5. **别为一个定位函数安装完整包。** 直接正常导入 coder 会进入 Aider 的模型、IO、Git 等依赖图；项目支持 Python `>=3.10,<3.15`，完整依赖包括 LiteLLM、GitPython、rich、prompt_toolkit、diff-match-patch、音频和遥测相关包。局部纯函数不需要它们。[项目元数据][project] [完整依赖输入][deps]

许可证是 Apache-2.0。[许可证][license] 如复制代码再分发，保留许可证和适用声明、标明修改，按许可证处理可能存在的 NOTICE；这是仓库条款核对，不是独立法律意见。

成本判断（工程估计，不是实测工时）：纯函数和提示的离线 POC 为低成本；真正成本在 OptoMind 自己的安全契约。至少要补：精确绑定文稿和版本、恰好一次命中、明确禁止/规定重叠、整批内存预演与一次提交、edit ID 去重和重放结果、逐项审阅 diff、原样保留公式/引文/Unicode/换行。对已有成熟文件层的团队，窄 POC 约半天至一天；可回归的严格执行层通常需数天，不能靠“pip 安装 Aider”完成。

## 离线验证说明及复现

执行环境：Python 3.12.14；未安装完整 Aider，未启动 CLI，未构造 `Model`，无模型调用、无费用、无生产文件改动。

先静态检查上游调用链。`tests/basic/test_editblock.py` 的 `setUp()` 构造 Model，`models.py` 有模型元数据网络获取路径，因此未直接运行整套 pytest。测试 harness 通过 Python AST 选择**原始函数、类方法以及原测试方法的未修改正文**并编译执行；仅隔离 import / setup、提供空 Coder / prompt 宿主，`apply_edits` 演示用本地文件 I/O 适配。udiff 仅载入对应纯函数链和同值预处理配置。Python audit hook 拦截 socket、子进程、系统命令和 urllib 请求；本次阻断事件为零。

- 26 项是“AST 隔离后的上游方法体执行”，全部通过，**不是安装完整项目后通过完整测试套件**。
- 27 个额外探针是本报告设计的输入，执行上游原函数/方法；不是上游测试，也不是另写一个执行器。主要决策结果已归纳于上面八项，不用数量代替覆盖质量。
- 本次没有测模型表现、网络/API、完整权限准备、Git 自动提交或所有 edit formats，也没有新增产品实现。

复现命令：

```sh
python --version
set -o pipefail
python edit_protocol_research/aider/offline_probe.py 2>&1 | tee edit_protocol_research/aider/offline_test.log
```

同目录交付：`offline_probe.py`、`offline_results.json`、`offline_test.log`、`source_manifest.json`、`source_integrity.json` 和 `upstream/` 原始源码。

[commit]: https://github.com/Aider-AI/aider/commit/5dc9490bb35f9729ef2c95d00a19ccd30c26339c
[docs]: https://aider.chat/docs/more/edit-formats.html
[registry]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/__init__.py#L1-L35
[eb-entry]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L15-L124
[apply-updates]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L2296-L2328
[fenced]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_fenced_coder.py#L1-L10
[editor]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editor_editblock_coder.py#L1-L8
[prompt]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_prompts.py#L120-L158
[markers]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L386-L398
[parser]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L439-L535
[match-chain]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L127-L187
[whitespace]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L243-L293
[disabled-tests]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/tests/basic/test_editblock.py#L50-L70
[perfect]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L146-L154
[repeat-tests]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/tests/basic/test_editblock.py#L278-L298
[filename]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L538-L599
[wrapping]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L335-L366
[dots]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L190-L240
[do-replace]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L364-L383
[similar]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L602-L628
[reflection]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L924-L944
[reflection-limit]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L98-L101
[io-default]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/io.py#L255-L257
[io]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/io.py#L453-L507
[udiff-direct]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/udiff_coder.py#L261-L279
[sr]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/search_replace.py#L434-L445
[sr-pre]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/search_replace.py#L528-L614
[patch-match]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/patch_coder.py#L54-L93
[patch-apply]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/patch_coder.py#L642-L706
[project]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/pyproject.toml#L1-L35
[deps]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/requirements/requirements.in#L1-L58
[license]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/LICENSE.txt#L90-L122
