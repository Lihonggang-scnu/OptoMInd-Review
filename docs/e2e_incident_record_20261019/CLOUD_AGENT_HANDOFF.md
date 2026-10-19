# 云端 Agent 脱敏交接摘要

## 当前终态与读法

最终 receipt 的 `completed_at` 是原始 UTC `2026-10-18T11:05:07.977191+00:00`，上海时间为 `2026-10-18T19:05:07.977191+08:00`。状态为 `complete_with_diagnostics_local_review_draft`，源码 `0133380ea82068ec8d9c28269bf9d0417d38bb04`，binding SHA `b4e9f56f191f2d4e1de211db08f0f60a221f42c0068005776c6382b5e0cdd2ec`。交付形态是 66 页、7 章、36 units、9 表、199 refs，PDF SHA `ecc410afaac21bd6215318b9c1a8fb99e317d239d7be5b313774343b1e7848f0`。

publication 仍有诊断：`LATEX_BUILD_REPORT.json` 为 `failed`，undefined citations `532/635`；波长 `@` 在 p47 表 9、p51 形成实际问号；`author_metadata_pending` 未闭合；三处数学 fallback 各含 U+0008，历史 FF 已归一化为换行。`scientific_allclear=false` 是验收状态。PDF 存在、returncode=0、测试通过、缓存复用或 0-call preview 都不能升级为投稿就绪或科学全绿。

费用仅按 final receipt：项目 cap 110、settled 100.821423、uncertain 0.1086312、reserved 0、available 9.0699458；账户 cap 300、settled 158.6437595、uncertain 4.6254988。897 是项目 settled ledger rows，不是物理调用；账户累计不与项目金额相加。`.885416`是V19前两个不同repair的小计，后续CH06失败repair`.462222`也是V19历史调用；V20纠正receipt本身`new_calls=0`。这些都已包含在最终累计，不能再叠加到总费用。

## 云端优先级：按受影响路径处理

### 1. append/full-rewrite 与缓存安全重放

在现有 writer/quality/article-edit 合同中补齐受影响消费者的判别和恢复保护：ordinary author、candidate、quality derivative、article edit、final publication 分开。full rewrite 若以 append 返回，受影响单元 fail closed；保留可用原稿和 pending，不停全书。U04 原始 335 字只保留 failed candidate 历史；只有实际 1553 字 sealed derivative 可进入装配。历史 quality `model_calls=1/2/3` 原样保留，不能被预览改成 0。

缓存重放要分层核对 source identity、canonical paper ID/DOI、source/card hash、message/body hash、chapter/unit、attempt、generation kind、profile 和 seal；这些字段分层校验，不能合成一个万能身份字段，状态合同仍独立。拒绝/失败/unknown 不得隐式变成成功，也不得因路径搬迁而重买同一科学请求。sidecar 只能来自同 unit/attempt 的 physically ancestor original writer result，并同时通过 UNIT_PACK/UNIT_INPUT map、body/result seal 和 stable identity 检查；不扫描磁盘根，不跟随无关 ancestor。

验收仅针对受影响单元：ordinary/candidate/derivative/append/invalid JSON 各有状态回归；36-unit assembly replay 必须保留原 body/messages/QC/seal/raw diagnostics，只在 assembly summary 增加经 IdentityIndex 实际验证的 alias provenance。

### 2. 科学拒绝与 namespace 防重买

同一科学拒绝、跨 namespace query duplicate 和 provider/transport error 必须分态。不要把科学拒绝自动重试成付费调用，也不要宣称已有统一 robust rebuy guard。V25 scoped ID 已是已实施的通用修复；未来只需把 namespace/legacy readonly 约束贯穿受影响路径。P157/P206 只有在 unit-local map 与唯一 canonical identity 同时成立时才解析到 P0157/P0206；raw `unknown_citations` 与 citation diagnostics 保留。

source/condition pairing 是科学接受边界：任务 ID、catalog、`resolved_from_current_pool` 和标题不能替代实际材料；算法不能承诺判定所有科学真伪。跨波段机制例按用户已接受的 role 和实际材料保留，不作宽泛 scope 拒绝。

### 3. owner/report 与 directed 生命周期

owner report 只投影 final supplied channels。早期 owner report 虚称未发送 pool 行已读；d23acb55… 已将 pool-only 行留在 unresolved，并保留 cache projection；历史所谓 323/191 行数没有独立证据，323.032 在 receipt 中是 elapsed seconds。以后验收必须同时读 source/condition payload、任务签名、Docling config projection 和 stable candidate ID。

directed claim要按现有正常run+resume恢复合同核对：`24695cf6…`已验收完整attestation、旧owner身份与原子版本比较，只对已证明未dispatch的claim显式恢复。缺marker本身不等于已付费或可任意重试，不得全局清claim/释放unknown。连续监督要用 fresh CIM/process state，不能用旧 CURRENT_PROCESS 文件判断 inflight/exit；误报不自动扩展为全书失败。

### 4. 出版残余

先修受影响的 wavelength `@` citation tokenization、undefined 532/635 和 author metadata；保留现有 literal math 投影与 raw audit。只有 bibliography、citation token、TeX/PDF validation、视觉检查和作者元数据都通过，才可称 submission-ready。不要因这几个局部残余重写科学正文或反复采样 Qwen。

## 最小验收包

1. 在无模型/无账本 fixture 复现 P157/P206：显式 canonical map 通过；缺 identity、namespace/DOI 冲突、无关 ancestor、伪造 `resolved_identity_aliases` 拒绝。原 UNIT_RESULT 的 unknown diagnostics/body/messages/QC seal hash 不变。
2. 对 affected-unit cache replay 验证 ordinary/candidate/quality derivative/append/invalid JSON 分态；U04 335 字不能 ordinary 化，1553 derivative 需 sealed provenance，历史 QC model-call 计数不改。
3. 对 owner projection fixture 验证 pool-only 未发送句柄不出现在 supplied channel；保持 unresolved；任务 signature/Docling config/stable candidate ID 必须匹配。
4. 对 directed gates 验证 decision→invocation→response/error lifecycle；无 marker 不计 provider call/费用。对监督状态用 fresh CIM，不从旧快照重启。
5. 出版 fixture 复现 201→199 bibliography guard、delimiter、三处 U+0008 literal fallback；额外验证 wavelength `@` 和 author metadata blockers。不要把 PDF returncode 0 当成完整通过。

## 计数与科学边界

1715 初检→600 准入→548 基础 A/B（160 旧+388 新），补材工作池 555；7 章/37 上层线索→36 units；V24 18 batches=12 accepted+6 rejected，124 selected contributions/107 unique handles。共享列表不可相加。所有报告必须分开 program、real model/cost、science 三门；`scientific_allclear=false` 和 pending 保留，不以本 handoff 触发全书 fail-closed，也不自动建立 claims framework/新科学评分。

## 交接限制

这是用户已要求上传的脱敏问题记录；云端目的地待提供，尚未执行上传。记录本身不包含实施生产修复或公开论文的授权。只索引 N/S 相对路径、字段摘要和 SHA；不包含 credentials、SQLite、完整论文、完整 REQUEST/raw response。任何 source 改动、provider/paid repair、ledger/binding/gate 变化、旧产物覆盖、publication release 或科学正文修改，都需要新的 root 级授权、同 commit/source 和独立 receipt。本次只写文档，未上传。

## Root 核对的首要未修工程点

`S/optomind_research/runtime/upgrade3/review_unit_writer.py:run_unit_completion` 的full-rewrite/overlap guard尚未实现，见E2E-034及证据`E85`。单次返回status=appended不足以准许拼接；已拒完整SSE也不能因存在原始缓存而在普通resume重放。当前containment为保留有用原稿/pending、正常命令无`--reparse-saved`/无retry，特定重解析先审。云端先用无provider、无账本fixture验证有效append、整稿重写、重复片段和拒绝缓存重放四种情形；不人工截取“正确片段”、不重新抽样。

本包只含文档及证据文件指纹，不附原始证据、源码或科学全文。进行源码定位/验证时需另取得对应commit的源码和获准的小型fixture；不要把本题N缓存/旧binding当作新题默认输入。

## 2026-10-19 追加：编辑要求与监督空档

本版追加的 E2E-055–057 是用户明确要求的后续工作，不是回溯后篡改旧验收：

- E2E-055 摘要无可见引文：摘要当前724字、14组/13 unique句柄；后续交付准备应隐藏可见引文，同时保留内部证据和source provenance。旧摘要不重写，状态 deferred_user_requested。
- E2E-056 引言起的编号映射：现有 delivery_citations.py 已有全篇canonical first-use map；新要求是在摘要禁引文后从引言重新定义显示起点，验证各canonical首次出现的新编号从1连续、复引用仍指同一paper、ref表同步。不能假设fixed [n]文本会因LaTeX样式自动改号；正文数字不得全局替换。
- E2E-057 摘要/引言职责：54字首句相同是可见结果；职责卡、conception和防重复指令均存在。已证缺口是共同轴、摘要读取prior parts以及结构/位置校验没有验证最终读者功能；LLM锚定只是未证假说。后续按职责功能验收，允许共享中心问题和术语，不能只做首句禁令、词面去重或追加全文重写。

E2E-058 是2026-10-18 01:32–04:32+08的十个heartbeat turn因API usage limit失败的监督边界。它不计provider/model call、费用或科学失败；需把heartbeat错误与fresh CIM/native receipt分开，不因缺少assistant decision自动重启或重买。C01原文SHA和短摘录在 CONVERSATION_EVIDENCE_EXCERPTS.json。

对历史BODY接缝的精确更新：CH02_U02标签未证仍按用户容忍继续诊断；CH03_U01 546字普通稿先免费0-call恢复，QC指出P02/P03/P04缺失，唯一1800字append后总2348字且quality仍pending；局部P0355/P0244保留。U04原始335字仍是rejected candidate，只有真实1553字sealed derivative可装配。CH02_U03被拒整稿重写的append/overlap guard仍未通用修复；普通续跑不带 --reparse-saved，不能靠再付费解决。

云端边界保持不变：只修受影响路径，继续保留原稿、失败、unknown、pending、费用和人工决定；不把科学拒绝、模型瑕疵和provider失败合成一类，不自动建立新claims框架。当前没有云端目的地，未上传。


## 2026-10-19 补充：M3最低600的用户触发

对话层补回了10月16日公开消息：用户先问A/B卡为何少（C04），随后明确要求最低600、不能因首篇15%阈值提前停止（C05），并同意补卡汇报质量（C06）。ROOT_M3_MIN600_ACCEPTANCE.json（E96，SHA 414185ccc8caaacc6331f91cf00f3091afc4fa9a85287077e013e4dcc1e9e954）确认免费修复后的169→600、3054 eligible、effective_min=600、paid/network=0和5项检查；后续548（160旧+388新）是基础A/B实际采纳池，不是600篇科学合格承诺。云端验收应分别记录eligible、selected、A/B adopted和science acceptance。

10月16日早段的肠道案例摘要/网站上传属于旧项目交付；本题记录不把它当作超表面稿上传授权。当前云端目的地仍未提供，完整本地过滤对话文件只作本地证据，不上传。
