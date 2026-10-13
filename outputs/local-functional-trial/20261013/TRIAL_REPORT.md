# OptoMind 本地测试事实记录

本轮完成版本保存、历史回放核对、环境配置及 Review、Article 小案例运行。以下记录执行结果、产物位置和未执行项目；修复顺序由云端 Agent 阅读源码和产物后另行提出。

费用按服务返回用量及中国区公开单价估算 **0.4619124 元**，共同上限 15 元，未决预留 0 元。四个明确 HTTP 400 按零 token 计费记录保留。模型只有 qwen3.5-plus、qwen3.7-flash；没有解析 API、完整 BODY、RCWA/FDFD 或实验硬件调用。本文件及对应产物按用户后续要求上传 GitHub。

**版本与环境。** 三仓库各自的原始增量、Windows 适配已经分开提交，完整提交号及原测试树见 [TESTED_COMMITS.json](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/TESTED_COMMITS.json)。后续 Docling、依赖声明与真实入口修复分别另存提交，最终提交与树见 [FINAL_TRIAL_ACCEPTANCE.json](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/FINAL_TRIAL_ACCEPTANCE.json)。三个工作树均干净；旧目录原内容未由本任务清理或覆盖。

普通 Review 用隔离 Python 3.12.4；Article/Torch 用独立 CPython 3.12.13 与 CPU Torch；桥接用单独环境。持久路径及启动器在 `runtime-alignment`，两个 optomind 发行包没有同时 editable 安装。启动器清除 PYTHONPATH、启用 PYTHONSAFEPATH，从 F 工作目录调用也解析到指定 D 源码。Docling 使用已存在的独立本地 CUDA 解释器和缓存，源码入口与产物指向 D，不混入 Review 主解释器。已安装依赖的兼容检查通过；未安装全部可选功能。

**历史回放。** 现有 ID_MAP 只有位置谱系，没有这份 packet 与录制的充分绑定；四项中 writer 仅三项。状态为历史回放不完整，不补造第四项、不按顺序重映射、不放宽校验。详见 [逐项审计](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/history-audit/HISTORICAL_BINDING_COVERAGE.md)。

**Review。** 真实问题为可见光消色差超透镜的带宽/FOV 色散极限与效率权衡。模型分出三项科学子问题。首次要求映射缺少 R4/R5；显式修订后，从模型返回的 `previous_plan` 包装提取计划并通过同一验证器。原失败响应均保留。真实检索得到 70 篇去重论文、182 命中：4 个查询完整成功、5 部分、3 因 429 失败，三个零返回均伴随请求失败。

两篇真实全文经新 Docling 正文批处理、表格/位置投影，生成两张新问题绑定卡片。一篇来自本轮检索，另一篇是交接本地集合的背景 perspective，未冒称为新检索命中。被选中精读的理论论文触发本地公式增强：6/6 refs 处理并保持 PDF/原始文档/输出绑定；结果仍是转录候选，不能当成科学证明。sidecar 复用已有证据，单独执行器缓存命中没有另行证明。

纠正精读的 0 思考预算请求错误后，规划运行至 level2，状态为 `stopped_after_level2`。阶段文件包含 CH1、CH2、CH3 三章提案，但最终共享纲没有章节数组，CLI 章节数为 0。最终材料把 P0001 标题写成 `Perspective on Achromatic Metalenses (Provisional)`，与卡片原题不同；另有“基于原理进行逻辑重构”但“不扩大结论不确定性”的表述。语义审计将 F1、F2 记为部分覆盖，F3 记为缺少直接实验证据。一次精读 fulfillment 为 partial，另一次因共享精读名额耗尽为 unmet，无 provider failure。未运行章内 arrangement、写作与出版。

证据：[新案例语义审计](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/review/evidence-audit/NEW_PILOT_EVIDENCE_AUDIT.md)、[纠正后的二级规划](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/review/planning-02/PROGRESSIVE_REVIEW_PLAN.partial.json)、[Docling 运行验收](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/docling-integration/DOC_LING_RUNTIME_ACCEPTANCE.md)。

**Article。** 同一冻结任务：空气/两层固定常数折射率 1.46 与 2.40/玻璃 1.52，各层 40–160 nm；450–700 nm 每 5 nm，正入射 s 偏振，种子 17，每轮最多 64 次求解。真实分析和规划暴露的采样间隔、否定求解器、省略号拓扑误判已经分别修复、检查、提交。失败尝试完整保留。

第六次入口尝试的首轮实际执行 20 次正向调用，最佳厚度为 **90.542547 / 109.552957 nm**。51 点反射率独立复算的最大绝对差 **8.67e-17**，未放宽预定 1e-8 容差。算术平均反射率 **0.0187995066（约 1.880%）**，低于冻结 3% 目标。

程序的目标报告为 **0.0188381061**，经复算确认采用同一网格上的梯形积分均值；算术平均值为 **0.0187995066**。真实反馈提出三层调整，外部试运行物理合同在执行前拒绝该方案。此前 run-04 的梯度适配器在预留 64 后报告消耗 168；该适配器接收 `maximum_forward_evaluations` 参数，但没有据此限制调用。本轮随后用外部配置限定单起点 8 步 Adam，并关闭 LBFGS 和额外全局搜索，64 次上限及种子 17 未变。run-06 主程序状态为 completed，第二轮为失败记录，第一轮谱独立复算通过。

证据：[独立数值验收](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/article/NUMERIC_ACCEPTANCE.json)、[逐点 CSV](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/article/INDEPENDENT_SPECTRUM.csv)、[原始真实循环产物](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/article/run-06/RESEARCH_RESULT.json)、[梯度预算与旧测试诊断](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/runtime-alignment/audit/planner-baseline/runtime-diagnostic.md)。

未执行升级前后或能力关闭对照，未运行完整回归。历史缺失 writer 响应保持独立记录；本轮未修改历史 ID 映射或补写第四项响应。本文不预先指定下一轮修复顺序。

逐步过程及组件检查见 [WORK_LOG.md](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/codex/local-integration-20261013/outputs/local-functional-trial/20261013/WORK_LOG.md)。最终机器记录绑定提交、输入/产物哈希、费用状态及未完成项。本轮到此停止。
