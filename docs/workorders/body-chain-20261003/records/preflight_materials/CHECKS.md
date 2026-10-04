# 第二停点验证

## 负责人最终选集

```sh
PYTHONUTF8=1 PYTHONPATH=/tmp/optomind-stage2-deps:. \
python docs/workorders/body-chain-20261003/records/preflight_materials/run_offline_controls.py
```

**290 passed in 28.69s（最终 fixture 更新后复跑）**，见 FINAL_CONTROLS.txt。该有界选集拒绝 socket 网络连接，覆盖第一停点、真实 reader/store/adapter、历史材料复用、卡片身份、首次案例、实际批次缓存和 writer 消费，不是整个测试目录或完整真实运行。

修前在未修改的归档代码上，第一停点原 164 项控制 **164 passed in 26.95s**，见 BASELINE_CONTROLS.txt；本次最终选集包含这些检查，不能相加。环境恢复使用临时目录中的 pytest 9.1.1、AgentScope 2.0.9、ftfy 6.3.1，没有改仓库依赖定义。

## 失败先行及独立复核

- identity/：F4A/F4B 的独立失败反例、修后实际章节/reader消息与 store/cache 产物；兼容搬目录和真实 legacy PROMPT 免读是正向控制
- cases/：最终同一 15 测试文件在基线 **10 fail / 5 pass**，修后 **15 pass**。首次全链离线反例证明模型输入已见可用精读但附加丢失；新 optional 参数缺失造成的 helper 失败单独标明，不冒充原始科学损失
- review/：3 个永久独立回归检查在基线 **3 fail**、修后通过；其 134 项相关检查通过。当前身份、正文/参考文献变化、旧 artifact 保留及实际下游消息均被检查
- 实现者另外运行的 157 项身份/相关控制、83 项案例/相关控制等与负责人选集重叠，不相加为“总通过数”

测试仅替换模型、网络及必要 token/账本构造边界；没有真实供应商调用。保存的是合成工程消息与选定落盘投影，不能冒称完整真实材料回放。

## 原有测试为何调整

三个既有测试文件作必要合同对齐，未删除实质断言：

- `test_directed_reading_real_store_contract.py`：不再假设实际任务 source_hash 永远为空；用真实 store 中的内容绑定 hash 检验 commit 不可覆盖、失败释放和任务归属
- `test_progressive_review_plan_material_reuse.py`：相同来源免读 fixture 补实际内容证明；候选真实 paper_id 与映射键一致，不再用异论文身份伪装兼容
- `test_body04_owner_material_handoff.py`：已明确错配的旧 A/B 应从活动拷贝隔离；增加原输入记录不被修改的断言

## 静态与范围检查

`git diff --check`、变更生产/测试文件 compileall 通过。PROMPT_BOUNDARY_CHECK.json 对照基线 AST：`_messages_for`、`_planner_instructions`、`_practical_reading_plan` 均相同。仅两个生产文件改变：`progressive_review_plan.py`、`directed_reading.py`。人工 diff 核对原协调及负责人→正式案例附加→编排→写作顺序未改变。

本轮无付费模型、真实检索/论文下载、完整 BODY 或合并。Git 和临时测试依赖下载属于开发操作，不声称完全零网络。

## 待本地验收

- 真实历史 snapshot/卡片/身份别名与既有 PROMPT 文件的兼容覆盖，尤其有证明与无证明的旧 artifact
- 内容确实变化时只重读相关任务，同内容搬目录不新增读取，旧文件保持
- 首次采用独立精读的真实案例是否进入正式细纲和 writer 输入；不能只查引用数量
- 案例批次固定 thinking=2048，通用 thinking 参数无效变化不应误判需重算
- 新进程复用的零新增调用仅指通过验证的案例批次/相同阅读任务；案例 fixture 的旧上游若因上下文变化重入，不据此宣称整条规划恢复零调用

通过上述局部验收后再共同决定完整真实实验。云端验证不能证明科学表述无误或全篇达到旧 176 引用稿水平。
