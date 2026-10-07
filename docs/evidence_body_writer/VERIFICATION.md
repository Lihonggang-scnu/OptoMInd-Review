# 第二轮写作层离线验证

## 基线和范围

基于 `lihonggang-dev` 锁定提交 `a5df7195ed90da5278011a6efd38ea2ef84690a4`。新增三个独立作者候选和一个全文读者精修候选、供材编译器、正式CLI、配置、提示词、测试和本地执行说明。旧BODY planner、负责人、案例、编排、unit writer及旧七路线算法未改。

本轮云端模型调用与付费为0。已有真实响应重放和受控返回明确分开；真实写作效果由本地下一轮完整稿决定。

## 已实际核对

- 七章真实作者返回经过新运行器、真实存储/解析/缓存，得到与历史完整BODY一致的文本和95项任务；重复运行命中缓存
- workbench真实首章漏表触发一次补写；受控补表仅用于验证接线，不是新的科学写作结果；原文逐字保留
- 正常stop的语义漏项在补写仍不充分时可保留暂定章节继续写后文，最终任务继续pending；网络、预算、截断和容量失败不触发自动连环收费
- 原读者三项问题通过离线地址转换检查新块协议，保住原来两项有效修改，无效第三组不抹掉其它成果
- 全文读者只收到一份完整正文；编辑材料按所指任务/正文引用组织，回读不会再把同一原始记录和同一批atoms重复塞入
- 正式CLI测试保留真实Qwen序列化、SSE解析和SQLite预算，只替换HTTP/凭据边界；作者实际传输的思考＋回答合计65,536，stream和超时参数生效
- 新60元账本与旧账本隔离，余额不足零发请求；已知占额核对后显式恢复，未知费用继续保留
- A/B中不同条件、未知字段、独立精读、工具答案、显式alias、同论文不同快照和综述转述均有消费者测试

## 实际输入对照

从校验通过的公开完整输入重新构造旧、新请求，使用相同计量方法计算UTF-8序列化字节：

| 对照 | 旧请求 | 新请求 | 减少 |
|---|---:|---:|---:|
| 全文一次作者 | 4,890,822 | 3,463,610 | 29.18% |
| 首章连续作者 | 2,257,864 | 1,089,441 | 51.75% |
| 首章工作台对照 | 2,390,117 | 1,089,441 | 54.42% |

新全文初始材料视图有4,655个完整对象、194个来源身份；完整池仍保留221个身份，额外来源可回读。所有23条相关工具材料保留。同身份同字段的精确值去重本身节省772,369字节；其它变化来自去包装、紧凑序列化、角色区分和显式任务范围选择。完整档案无损，模型默认输入为选择性投影。

这些是字节而非模型token。本地用已有tokenizer预览决定整篇是否容纳；不为验证容量启动付费请求。

## 复现命令

```bash
python -m pytest -q tests/upgrade3/test_writing_evidence.py tests/upgrade3/test_evidence_body_writer.py tests/upgrade3/test_scoped_body_revision.py tests/upgrade3/test_evidence_body_cli.py tests/upgrade3/test_evidence_body_integration.py
python -m pytest -q tests/upgrade3
python -m compileall -q optomind_research/runtime/upgrade3/writing_evidence.py optomind_research/runtime/upgrade3/evidence_body_writer.py optomind_research/runtime/upgrade3/scoped_body_revision.py scripts/upgrade3/evidence_body_writer.py
git diff --check
```

专项最终82通过。完整基线和最终代码的结果、失败集合与日志hash见 `TEST_RESULTS.json`。基线全套1841通过、17失败、10子测试通过；已在本次单独基线工作区实际复测，不仅沿用上次记录。

## 仍需本地确认

- 新材料投影与自主选择是否保留每个实际任务最有价值的材料；本地亲读选择和全文，而非只看数量
- packed_whole实际token是否可容纳；dossier是否充分降低上下文并改善论述，增量策展费用是否值得
- 完整稿的条件保持、深入比较、跨章增量和语言风格；少量科学措辞误差不要求无限返工
- 本地原生Windows与真实供应商的长流式响应、超时和用量。本轮离线HTTP测试只证明接线，不代替下一轮真实运行

公开归档的部分旧MESSAGES分片损坏，且公开脱敏book不再匹配当时生产seal；不能拿它直接当生产CLI缓存。真实原响应重放走校验后的公开记录与正式运行库，CLI另有可完整重放的本地fixture测试。本地请使用原始完整manifest与材料，勿伪造seal或混用快照。
