# 本地 Agent 下一轮全文写作测试指令

## 目标和预算

请先读同目录 `SEVEN_ROUTE_REVIEW.md`，再核对本轮源码和实际输入。目标是找到成本可接受、机制解释与全文组织都足够好的完整 BODY 写作层。产品质量优先；不要为了实验形式反复买相同调用，也不要用人工科学问题清单替系统完成自主判断。

本轮是**新增、独立60元人民币**，四个候选合计使用这个额度，包含选材、作者、回读、补写、读者、编辑和失败占额。原高级60元与朴素40元账本继续保留，不挪用、不清除原未决费用。不要把新60元理解为每条路线60元。

从 `lihonggang-dev` 拉取本次已发布代码，记录完整SHA；不要覆盖你的未提交改动。当前研究依据是归档 `a5df7195ed90da5278011a6efd38ea2ef84690a4`。实际测试使用此次新代码，不能继续用上一轮344f/50cc的旧入口代替。

正式入口：`scripts/upgrade3/evidence_body_writer.py`。
配置：`config/evidence_body_writer/plus_first.json`。

## 先确认方案确实落实

新材料编译器位于 `optomind_research/runtime/upgrade3/writing_evidence.py`。它保留全部原材料档案，把同一来源同字段的相同值合并，在模型视图中区分写作职责、可归属材料与先前规划解释。发现与条件保持在完整语义对象中；不同快照有对应标识；工具答案和可用综述转述保留。没有600字符前缀截断。

输入必须仍是上一轮已批准的完整细纲和匹配编排/packet/材料。复用原始科研材料，不重新规划、不重新检索、不把尚未正式合回的加强候选混入这轮。先用上一轮 `SOURCE_INPUTS.json`、`CLI_CONTEXT.json` 和本地原件定位完整 manifest、plan、tokenizer、预算系统。公开脱敏输入用于审查和离线重放，本地正式运行请消费完整原件。

请亲查以下内容已在实际请求中：
- 当前完整任务与任务ID、需要解释的关系、案例用途、决定性条件和反例
- 跨章只读职责；连续写作时真实已写前文，不是上一层的总结
- A/B中具体科学内容、独立精读、工具补充、已确认alias和综述转述
- 中间层选材之后，实际送给作者的是选中的完整对象；设计和限制一并保留；未选原件可回读

前七组亲审意见仅用于最后评价，不能注入作者、策展者、读者或编辑器作为答案。程序自身的通用读者诊断可以进入编辑器。

## 四条候选及顺序

1. `packed_whole`：本地编译后的完整任务和材料一次写全文。先用本地tokenizer免费预览；能容纳再执行。仍超限就保存结果并跳过，绝不删后半篇或裁材料来冒充成功。
2. `packed_continuous`：按自然章节连续完成全篇。当前任务和相关材料充分供给，并读取实际前文。只在容量不够时按完整任务边界分段。出现正常结束但明确漏表/漏任务时，一次局部补写，原正文保留。
3. `dossier_author`：Plus先根据任务自主选择完整证据对象，程序保留其设计、条件、限制与归属；随后作者写全文。如果选后全文输入足够小，则一次成文；否则明确记录为按章连续写作。策展调用、回读和补写全部计费，不把它们藏在“中间层免费”里。
4. `scoped_revision`：复用一份完整 BODY。独立读者读全文和任务意图，自主指出值得修改的问题；编辑器读取相关证据并按程序提供的块ID修改整段。无需重抄长锚点。它可以解决比较没展开、条件丢失、任务书口吻和同用途重复，不能只是机械替换几个词。

建议先免费预览，再拿到 `packed_continuous` 的完整稿与 `dossier_author` 的完整稿。`packed_whole`如果通过容量预览可提前做一次；`scoped_revision`选择一份最值得修的完整稿，不对每份都自动编辑。旧连续作者完整稿是现成基线，也可用于检查编辑模块，但必须把“旧底稿编辑”与“新路线正文”分开报告。

建议资源分配参考：连续作者约15元、中间层作者约20元、全文编辑约12元、整篇一次尝试及恢复约13元。这些是投入优先级，不是要求花满或另设四个账本。可依实际预览和已生成质量调整，60元是唯一总上限。预算不足先保住完整稿，不为凑四个成功结果强行续费。

## 正式运行示例

在仓库根目录的 PowerShell 中，先把以下路径变量解析为已有本地真实文件。不要猜另一个版本的输入：

```powershell
$manifest = "已核对的上一轮完整SOURCE_MANIFEST或输入manifest路径"
$plan = "对应已批准CURRENT_PLAN.json或DETAILED_REVIEW_PLAN.json路径"
$tokenizer = "已有本地Qwen tokenizer.json路径"
$root = "outputs/evidence_body_round2_20261008"
$ledger = "$root/new_round2_budget.sqlite"
$config = "config/evidence_body_writer/plus_first.json"
```

先记录输入版本，再预览：

```powershell
python scripts/upgrade3/evidence_body_writer.py --manifest $manifest --plan $plan --prepare-manifest "$root/PREPARED_MANIFEST.json"
python scripts/upgrade3/evidence_body_writer.py --manifest "$root/PREPARED_MANIFEST.json" --route packed_whole --config $config --tokenizer $tokenizer --output "$root/packed_whole"
```

新程序的 `--budget-limit` 是该新账本的绝对累计上限。每条路线使用同一 `$ledger`；新标记文件阻止误接前两轮账本。只有带 `--run` 才允许真实调用：

```powershell
python scripts/upgrade3/evidence_body_writer.py --manifest "$root/PREPARED_MANIFEST.json" --route packed_continuous --config $config --tokenizer $tokenizer --output "$root/packed_continuous" --budget-ledger $ledger --budget-limit 60 --run
python scripts/upgrade3/evidence_body_writer.py --manifest "$root/PREPARED_MANIFEST.json" --route dossier_author --config $config --tokenizer $tokenizer --output "$root/dossier_author" --budget-ledger $ledger --budget-limit 60 --run
```

`packed_whole`实际容量合格时，用它自己的同一输出目录、同样账本追加 `--run`。不要重复执行已成功调用。

选择完整底稿后，独立读取和精修：

```powershell
python scripts/upgrade3/evidence_body_writer.py --manifest "$root/PREPARED_MANIFEST.json" --route scoped_revision --draft "$root/packed_continuous/FULL_BODY_RESULT.json" --config $config --tokenizer $tokenizer --output "$root/scoped_revision" --budget-ledger $ledger --budget-limit 60 --run
```

若明确复用旧完整稿而本地路径或输入指纹变化，先核对原任务、身份和来源。`--draft-book`可指向那份底稿的原始封存输入，`--source-mapping`提供 `old_input_hash`、`new_input_hash` 和原因，程序仍检查任务与身份。不要靠篡改旧hash绕过不同细纲的混用。

凭据继续用现有本地安全方式，必要时使用已有CLI的 `--key-file` 本地定位参数；不上传、不打印密钥。没有本地tokenizer时，字节上界可能误判容量，请先找到已有tokenizer，不能把字节当真实token。

## 恢复和异常

- 正常同输入重跑复用已成功阶段；每个实际阶段请求、参数、返回、用量和解析结果落盘
- 已收到raw但解析中断，可以离线恢复；修改代码导致缓存合同变化时，不假设旧阶段自动复用，先看实际缓存计划
- 网络超时或未知费用保持未决占额；核对供应商记录后，再决定是否显式 `--retry-failed`。不能连环自动收费重试
- 1800秒流式无活动超时与3600秒总限时分开；等待期间及时报告进展/阻塞，不让用户一直等到最后
- 正常stop的明确任务遗漏只做一次有材料的定点补写。失败保留正文和未决任务；不要将未解决状态藏成成功
- 编辑使用原正文hash和确定块ID；一个问题的关联修改作为一组处理。无效组不抹掉其它已经有效的修改，不做模糊猜改
- 新尝试失败不覆盖旧有效版本。请核对 `selected_version` 与当前运行状态，报告正在展示哪份稿

## 评价和交付

先检查实际执行是否符合方案，再读完整稿。所有候选最终比较对象都是完整 BODY，不用第一章的表现代表整篇。

请先自行阅读全文，评价：
1. 已验收细纲中的机制关系、代表案例、实验条件、比较、阴性结果，是否真正形成解释
2. 材料中的写作说明有没有变成成文；没有把文章写成任务完成汇报
3. 同一研究跨章复用是否有新用途；减少重复时有没有删掉有价值的设计和条件
4. 后半篇是否充分展开，整篇能否持续回答用户问题
5. 中间层筛选省下了什么，遗漏了什么；回读是否真正发生及带来收益
6. 编辑前后完整正文是否更好，编辑增量和生成全稿总费用各是多少

不要让另一个自动评分结果代替亲读；可以辅助评价，但必须核对它归属的文本。允许适度模型误差，不为某篇题目硬编码纠错规则，不追求零错误。发现通用接缝问题先记录实际输入输出；未经必要判断不要偷偷更换方案或人工修稿后称模型成功。

完工后上传本轮全部四条候选状态到GitHub，包括零付费阻断、失败、部分完成、未执行原因。保存原始请求/响应与实际参数、选材对象和回读轨迹、完整/部分正文、补写/编辑前后、失败/恢复记录、费用与未决占额、输入/源码hash。采用UTF-8字符安全分片，上传后逐个校验，避免上一轮部分请求分片损坏。

继续在 `lihonggang-dev/docs/acceptance/` 归档，不扩张分支。不给公开包放密钥、原始账本数据库或权利不明论文全文；删节请求标清范围，原件在本地保留定位/hash。报告锁定提交、阅读入口、四条实际状态、总费用及你亲读后的推荐。完成后停止新增调用，交云端审阅。
