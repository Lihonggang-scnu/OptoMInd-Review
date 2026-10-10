# 分支整理与恢复依据

**历史定位（2026-10-10追加）：**本文件保留10月5日分支收敛、备份和初次源码整合记录，不是当前算法冻结清单。后续开发仍只用 `lihonggang-dev`；当前完整链路与正式baseline见 [PIPELINE](PIPELINE.md)、[RUN_GUIDE](RUN_GUIDE.md)。正式接入受测源码为 `638eb93cdb37fc95cb917130f69a5b16df3549b7`，固定对照归档为 `2618319e3fe2a085acf11a66e6f6c66c73a9c135`。本次收尾未修改main、归档历史或重新整理分支。

下面“装配pending阻断”描述的是当时状态：本轮只在完整BODY选择路径中区分终结科学诊断与缺失生成/文件/步骤，保留诊断而允许可交付稿进入消费者。history/plan缺录制、受限导入门槛不变；具体状态和退出码以RUN_GUIDE当前操作节为准。

## 本轮目标

保留原 main；新版只设 lihonggang-dev 开发入口；旧版本归入 archive/history。目标为三个分支，不以丢弃历史或混合旧算法实现简化。

## 来源核对

重新定位并阅读了用户提供的本地 Agent 工作记录相关原始设计及逐项交付：39,518 行，3,229,136 字节，SHA-256 `54ecd84036f6cea5929a9bced8072f4dd66493638f7129859b405f4defff3ab5`。原记录含私人工作上下文，不随此次 GitHub 整理上传。

记录1538–1541行明确 M1 query_plan、M2 candidate_corpus、M3 scholarly_skeleton；2713行提出本地材料工具层；10472–10488行明确前三模块→材料层→M4阅读/A-B卡片→规划。初次独立发布 a61ecca 后改名 review-v2，main 原始提交 e528f7e 保持旧版。

当前完整前置链仍在最新 BODY 分支内，不是把前三模块遗失后只剩 BODY。此次不从 main 回拼，也不把旧并行 PartsPlan 规划改造带回。

## 2026-10-05源码选择（历史记录）

- 主体：02c018bdd440cc061ddedbb5aef07b115e7de081，继承 BODY40 已验收身份/引用/alias 修复、A/B/C 实验，以及本地固定问题协议/跨平台测试小修
- 独立首尾：dc661cf47bdb8ec70a348c02bc8f15a0b40253f4 的既有两个 runtime、schema、CLI、文档及测试，有界接回现有 delivery
- 保留当前装配 pending 阻断；仅显式 post_body 模式进入独立首尾，未改变 BODY planner/编排/writer/首尾通用提示词
- 两处旧 serial 测试的合成装配输入补充 complete/resolved 字段；离线 socket guard 前预载标准库 ssl，避免测试自身导入顺序失败；增加默认旧路径及新模式 pending 控制
- advisor/ 从当前工作树移出，原文件在归档固定提交中完整保留；docs/acceptance 与 docs/workorders 有10个测试依赖，保留原路径，不为美观破坏回归

这些是既有能力的整理和显式接合，不是下一轮科学质量优化。局部修订、首尾写作及全链科学质量尚不能据此重新认证。

## 备份与可恢复性

清理前：34 个远端分支，83 个可达提交，无 open PR；未发现子模块或标准 Git LFS 指针。原 main：`e528f7e5503c087a327ed5739ff4d59d40cf45ee`。

已在云端设备保存独立 mirror 和全历史 bundle：

- 文件：OptoMind-before-cleanup-20261005.bundle
- 大小：205,306,696 bytes
- SHA-256：67ef1319d830243dd9af5a7369a04cfa4b39ce86f629eed113e81246680170bd
- git bundle verify 通过
- 从 bundle 新建 mirror，清理前全部 refs 逐项一致
- 恢复仓库 git fsck --full 通过

远端 archive/history 固定归档提交：`311796aba823e8041aa250402293a1230a1f27c4`。七个父历史覆盖清理前全部34个分支末端，已逐一检查可达性，无缺失。该归档提交的工作树只含索引与恢复说明，不进行代码合并。

索引：本目录 HISTORICAL_BRANCHES.json 和 [远端归档](https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/archive/history)。固定 SHA 链接保持可读；旧 branch-name URL 删除后应改用索引中的 SHA URL。普通 clone 仍会下载可达历史，删除工作树资料不代表仓库历史体积变小。

## 删除旧分支的约束

必须在新开发分支发布且备份/归档验证后执行。只删除索引中列出的非 main 旧名称；任何 tip 变化、新分支或目标分支不符均停止，不能强行覆盖协作者更新。使用可恢复记录与按 tip 的并发保护，不作历史重写或垃圾回收。

此文记录整理方案和已完成的备份验证；远端是否已收敛到三个分支，以最终 ref 清单为准，不能只因文档存在就宣称完成。

## 本轮实际离线检查

- 独立首尾、接合及装配阻断控制：225 passed
- A/B/C 修订专项：92 passed
- 既有 BODY 有界控制：557 passed
- 修改 Python 文件 compileall、git diff --check 通过
- 逐项核对核心 M1/M2/M3、材料层、A/B、BODY planner/编排/writer/汇编源码与02c018b逐字节一致；见 CORE_SOURCE_INVARIANTS.json

这三组有重叠覆盖，不能相加宣称为互不重复的全仓库通过数。没有付费模型调用、真实检索、完整 BODY 运行或新的科学质量验收。

若云端无法操作分支删除，可由本地已有 Git 登录态执行 `scripts/maintenance/consolidate_remote_branches.py`。必须提供本轮最终 lihonggang-dev 完整 SHA 与全新的备份目录；缺省只验证，显式 --apply 才删除。脚本重新备份、恢复核对、检查全部历史可达性，使用原 tip 的 lease 和 atomic push；若远端新增/移动则停止。不要将自己的密钥发送给云端。

并发边界：待删旧分支使用服务器端 lease，整批删除为原子操作；三个保留分支只读、不写。保留分支若在最后预检与删除之间被他人移动，最终检查会报告 needs_review，不能声称这类并发也由删除 lease 原子阻止。归档与本地备份仍保留原历史。

清理脚本额外通过20项本地临时 Git 仓库检查：正常验证/删除、完整备份恢复、保留 main/新开发/归档和 tags、旧 tip 变化、原子拒绝、新分支并发、幂等与目标仓库限制。实际发现并修复了继承 remote.origin.pushurl 可能重定向删除的风险；现在显式推送到指定仓库，且拒绝 fetch/push URL 重写。检查结果见 CLEANUP_SAFETY_TESTS.json，可用 scripts/maintenance/test_consolidation_offline.py 离线复查；它仅允许 file 协议操作临时仓库。
