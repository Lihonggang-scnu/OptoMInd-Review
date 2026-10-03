# WO04 本地真实验收入口

状态：**READY_FOR_LOCAL_04**。本地验收者：**GPT 6.1 sol**。

- 分支：`body04-material-handoff-cloud-20261003`
- 起点：`1b4f7b6a7c0dafb6f7dfe59f46980480f44d993f`
- **代码、测试与完整分项记录提交：`78d6d12975201f308c3b0c87018715dc6bec6cc6`**
- 实测代码树：`2f34175a50686bff4db22cb0638382b6274107af`
- 本文件由随后纯文档提交加入；代码验收请锁定上述实现SHA。最终远端tip在完成消息中提供

## 独立判断

已亲读归档 `4897ad0edd98b8d8f3013c57aafd3f0a59e03228` 的交接、亲审、三份原始解析结果、请求元数据及费用/恢复摘要。R2已经解决此前对点供材漏读，值得保留。模型过严判定、实验条件混淆和附带扩大仍有实例，本轮不把它们手工修进生产提示词，也不宣称整篇质量已恢复。

进一步核查代码后，选择工单04的材料准入、正式packet、引用目录及晚到选材接缝。已存在的当前身份映射和R2阅读机制保持，未另造身份系统或planner。

## 本轮实际变化

1. 负责人现在依据实际内容而非“有来源行”接受来源；嵌套paragraph briefs同样校验。此前已提供的候选被负责人采纳后，其材料进入正式packet，恢复时保留；综述转述加有效原始研究身份无需另备自身A/B或全文。
2. 工具独有来源能进入目录和相关单元的引用集合。多来源综合仍保留完整来源关系；单元专属材料不会折叠后泄漏到其他单元。未知或冲突身份保留/明确拒绝，不能借用同号P句柄。
3. 晚到可用论文在正式案例追加前获得相关章节选择机会。已知用途直接作增量路由；没有章节语境时只用现有router处理该增量，缓存独立。原路由不重跑，不要求新增论文全部成为案例。
4. 补上交叉复核确认的ID别名、身份-only行遮蔽材料和错误card_path带入他文内容的接缝。既有实质材料保留，冲突显式报告；普通缺省卡片格式仍兼容。

生产修改仅三文件：progressive_review_plan.py、chapter_arrangement.py、review_unit_writer.py。编排/写作修改限于材料、来源目录和作用范围接线；BODY科学提示词、写作任务策略不变。没有表格数量门槛，多个任务合成一张合适的表仍允许。

## 推荐阅读顺序

1. [总记录](../04.md)
2. [负责人材料交接](OWNER_MATERIAL_HANDOFF.md)，查看before/after_owner_result.json、after_formal_packet.json及两份owner_messages
3. [写作者来源交接](WRITER_SOURCE_HANDOFF.md)，查看WRITER_SOURCE_BEFORE.json与WRITER_SOURCE_AFTER.json中的实际文件/消息
4. [晚到来源路由](LATE_SOURCE_ROUTING.md)及LATE_SOURCE_ROUTING_EVIDENCE.json
5. [实际最终装配接缝](INTEGRATION_SEAM_EVIDENCE.json)

以上均为明确标注的合成工程证据。owner的after_writer_input.json是既有反馈流程传入writer回调的packet，不能冒称真实LLM请求；另外的writer与integration证据才使用生产unit_messages构造器。晚到来源四个阶段接线测试替换了工具循环和协调回调，另有孤立helper/真实router缓存测试，区别见记录。

## 验证结果

- 根节点最终有界离线控制：**339 passed，2 deselected**
- 包含已有251项R2控制、50项新增测试、38项已有相关writer/BODY控制
- 独立交叉复核：101项相关交接/恢复测试通过；确切失效反例已逐一重试
- compileall、工作区和暂存区diff-check通过
- planner instructions/messages、editor/writer prompt loaders、unit_payload及unit_messages的AST与基线相同
- 全局协调及负责人修订→正式案例附加→编排→写作顺序保留；未加入案例后整章返写
- 云端零付费、零真实学术检索/论文下载、零密钥使用、零真实全规划/正文生成

复现命令见总记录与run_offline_controls.py。只有GitHub归档获取/发布发生网络访问。没有合并受保护分支。

## 本地下一轮应验证什么

优先沿用已认可的真实材料与受影响章节，检查来源内容而非引用数量：

- 一个确实被采纳的候选，其研究条件和比较关系能否贯穿负责人→正式packet→编排→writer实际输入
- 综述转述原始研究的身份和内容能否正常引用使用，不强制补读自身卡片、不降权
- 晚到论文是否只进入相关章节的案例选择输入，并由模型决定取舍；未知章节的增量router仍受现有预算控制
- 同输入恢复应不重复调用；失败和身份冲突不应把已成功章节退回粗稿
- 最后评估模型是否真正展开机制/条件/比较；这仍是内容验收，离线测试不能替代

剩余边界：内容存在不代表内容正确；未解决身份不会自动获得新句柄；原始全池路由不因新材料被无条件重算；有界读取和LLM局部误差仍在。下一阶段须用户另行授权，本轮停止，不进入05–07。
