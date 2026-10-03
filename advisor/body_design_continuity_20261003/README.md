# 云端修复配套核验材料

生产代码核实基线：`93d82392aab03718cf463fee09751b8b19a743fa`。
本目录只增加核验材料，不包含生产算法修改。实施入口见 [工单 README](../../docs/workorders/body-chain-20261003/README.md)。

## 先读什么

1. `current/ROOT_ADDITIONAL_FINDINGS.json`：独立表格被解析丢弃、已读候选被误判为无材料。
2. `current/retrieval.json`：首轮空查询、跨阶段重复研究、补充重试撞旧目录。
3. `current/planning.json`：全文主张错接、恢复和位置 ID、空分批合并、反馈遗漏；也保留晚到材料检测实际有效的反例。
4. `current/materials.json`：旧句柄、工具引用目录缺失、晚到路由覆盖。
5. `prior/`：前一轮 R1–R5 及附带问题的离线复现结果。

这些记录是**缺陷复现结果**，不是修复通过证明；其中值为 true 可能表示 bug 存在。人工整理的结论在工单 references 下，不能覆盖本目录原始结果。

## 云端可以直接运行的两组离线探针

从仓库根目录执行：

```bash
python advisor/body_design_continuity_20261003/portable_probes/planning.py
python advisor/body_design_continuity_20261003/portable_probes/retrieval.py
```

只改了原探针的工作区定位与临时输出位置；真实 planner/cache/CLI/reader 接缝保留，仅模型和外部调用边界采用替身。运行无需密钥，输出到系统临时目录。2026-10-03 本地 Windows 上两份适配脚本均实际运行结束，复现结果与归档一致；尚未在云端 Linux 运行。修复后部分输出应改变，需另写回归断言，不能以退出码 0 当作修复成功。

`original_local_probes/` 保存原核验脚本，包含历史 F 盘路径；是阅读与改写回归用的参考，**不要在云端原样执行**。其中依赖完整本地历史运行的部分无法仅凭本包重跑。可从当前代码构造最小夹具，或读取归档结果；不得伪造本地历史资产可用性。

## 历史补丁如何使用

`historical_reference_patches/` 提供 9 份本地历史提交的相关生产文件差异，解决云端未必能取得这些 Git 对象的问题。它们不是补丁队列，**不要整体 apply/cherry-pick**。按合并方案的取舍表，只复用当前缺失的行为；保持当前 BODY 顺序及独立后置首尾路线。

## 哪些材料没上传

- 没有 API key、密钥文件、认证配置、授权请求头或带签名下载链接。
- 没有 SQLite 数据库、假密钥文件、论文 PDF/全文或完整原始缓存。
- `current/HISTORICAL_ASSET_LOCATIONS.json` 是本地资产索引，不表示这些文件已附。原始记录中的本地路径只用于说明来源。
- 已有基线阅读包在 `advisor/body_chain_repair_20261003/`，同一分支可直接读取。其他大体量历史运行仍以本地保留为准。

## 范围与提交

当前发布只含工单和筛选后的核验材料。实现者从本次交接分支提交新建自己的修复分支，先做 00–01，提交局部修复与阶段记录后停止，等待用户下一次授权；不运行付费或全量 BODY 测试，不修改 main/review-v2/lihonggang。
