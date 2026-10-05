# 本轮受测代码与链路恢复说明

这是历史验收归档，不是下一轮生产缓存。首尾模块、全文编辑与出版没有在本轮调用；不要将 BODY 当作完整投稿稿。

## 代码从哪里恢复

- 实际运行时 HEAD：fbf6f79328d533af2563678fc2d0e81364595c44；分支 body-full-staged-acceptance-local-20261004。
- 实际受测时还包含 8 个本地修改/新增文件，不能仅 checkout 原 HEAD 就称受测版本一致。
- 这 8 个文件已原样保存为源码提交 7e293b63603157e741ec9b68f30565fcb4065f8a（父提交即上述基线）。从该源码快照建立新工作分支，可以取得本轮最终测试的小修与对应测试，不必猜测如何应用零散 patch。
- code_delta/CODE_VERSION.json、git_diff_from_tested_baseline.patch 与 files/ 保存原差异和新测试。patch 只表示 tracked 差异，新测试另有文件快照；优先用完整源码提交恢复。
- 归档分支上后续提交只整理资料与说明，不新增正文算法。

## 实际生产顺序

provisional_scope → level1_tools → level1_outline → source_routing → chapter_proposals → harmonized_scope → level2_tools → finalize_chapter_scope → chapter_need_analysis → chapters_tools → chapter details → whole-plan coordination / owner revision → case_groups append → arrangement → unit writer → offline BODY assembly。

以本轮保存 RUN_STATE 的 completed_stages、分章 cache 和 ROOT_01–ROOT_28 为具体依据。章细化和负责人恢复记录不一定各自占 completed_stages 的独立标签，不能由数组少一个名字便断言漏跑。

关键：当前 planning_revision 路径在 progressive_review_plan.py 约4920行先完成全局协调/负责人复核，再增加案例。名为 _post_case_review 的方法以空 case_record 在案例选择之前调用。案例之后不会再做一次可能压薄或归档案例的整体 BODY 改写。不要按方法名称推断顺序，也不要把其他历史方案当作本轮事实。

## 输入、命令与模型

- planning/input585/INPUT_POOL.jsonl 是本轮585篇A/B输入，不是旧 planner 答案。
- 五份兼容的历史定向精读是显式 prior-reading 输入；旧规划/正文仅作比较，不能作为本轮生产PLAN/cache。具体路径见 run/acceptance_root/RUN_CONFIG.json 与 MATERIAL_PROVENANCE.json。
- 初始命令见 COMMANDS.md、RUN_CONFIG.json；它们是准备/起点记录，包含“未付费、余额40”等历史状态，不是最终运行结论。
- 实际后续恢复命令、参数、PID和日志对应各 CURRENT_PROCESS_*.json。主要驱动为 staged_driver.py、continuous_driver.py、resume_after_handle_fix.py；源码正式 CLI 仍在 scripts/upgrade3/ 下。
- 配置中 planner 为 qwen3.5-plus，chapter/reader 为 qwen3.7-flash；writer 实际使用 qwen3.7-flash。配置、实际响应 returned model 和计费 usage 均已附，价格估算不能代替账本实际费用。
- 主要运行参数：planning-revision；planning output32K/thinking8192；writer output12000/thinking8192/timeout900、max_material_chars_per_source=0。具体每次命令优先于此概述。
- 本地API key仅为路径引用，值未附。复制这份归档不会使云端获得真实调用权限。

## 产物选择与恢复边界

- 最终装配显式选 writer/repaired 的 Ch2、Ch3 与 writer/live 的其余5章；body/body_assembly_final/batch_input/BATCH_JOBS.json 列出29个实际结果文件。
- Ch2/Ch3只离线修复可确定的引用格式，原始付费消息/响应仍在live目录。不要误把reprocess记录当新付费生成。
- 第四章负责人修订未完全成功，保留原有效任务：planning=partial；29单元装配齐全仅表示assembly=complete，problems_resolved=false。
- Ch6_U4未解析数字引用、Ch7_U02的Q01以及科学归属/重复问题均未人工修正。主稿应阅读HANDLES版本，数字派生稿不作投稿交付。
- 公开资料删去了权利未明论文全文与原始下载载荷。含全文的实际请求仅发布明确标注的脱敏副本；原始字段长度、hash、来源路径保留。未改本地原件。A/B、精读认识、模型输出、任务与身份均尽量保留。
- 部分全文索引/数据库仅有本地索引与元数据；要在另一服务器重新真实运行，应自行取得许可材料、重建索引、配置本地key并另开获授权的运行目录。不能宣称公开归档包含足以逐字重放所有原始全文的输入。
- 大JSON提供gzip完整副本和阅读投影；脱敏gzip会明确标记。只看summary投影不能代替读实际章节计划。完整可读细纲另见 planning/FINAL_PLAN_FULL.md。

## 费用与质量停点

最终实际费用21.892783元，历史未确定占额0.228144元，40元预算剩17.879073元。run/BUDGET_LEDGER_ROWS.json/CSV 保存逐调用原始计费数据；reserved amount的历史总和不是实际消费。

根智能体按阶段停下亲审，并读完29个真实单元；具体判断在run/acceptance_root/ROOT_*.md。覆盖基本恢复、但未宣称普遍优于旧版或投稿就绪。规划/写作/装配的不完善都有记录，后续阅读者不应仅凭测试数或complete字段判断科学质量。
