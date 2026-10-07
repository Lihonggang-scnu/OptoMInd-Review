# 按需细纲加强：轻导航、完整回读与分步费用

仅作用于显式按需加强入口，不重新运行 BODY。原材料池、身份映射和细纲副本继续保存在本地；模型视图不再把整池长材料当目录发送。

## 实际运行

1. Plus 读取完整可编辑细纲、第一层机器焦点、相关只读职责、全池身份/题名/DOI/材料类型/读取入口/任务关联。任务相关材料展示已有结构化发现及对应条件、限制，不取 JSON 前缀。
2. `material_requests` 支持按身份或位置完整读取、`catalog_search` 全池关键词/逐字短语搜索、`catalog_page` 分页读取，以及 `readonly_unit` 展开只读职责。分页和搜索记录剩余数量、下一请求。搜索不依赖当前细纲是否引用该论文。
3. Max 只接收实际读取的完整材料；其余来源留轻导航。互补材料不丢，重复完整记录用请求内指针复用。允许一次补读和合理 `no_change`。综述转述内容与原始研究身份仍可参与，不要求自身 A/B 或全文。
4. 选材原响应和解析结果立即落盘。每次真正调用前由原客户端预留费用；负责人预算不足时保存选材，恢复不再付费选材。不以“读取全部目录”的估算作为启动条件。
5. 正常任务保持一次选材、一次负责人调用。实际选中内容超容量，复用规划器的完整记录加权分批助手；每批读取共同任务和轻导航，并以此前有效细纲继续修订。成功批次按输入、材料及参数签名恢复，不额外调用合并模型。

单份完整记录或任务背景本身不能容纳时返回 `capacity_blocked`；不裁剪科学内容。超限批次出现单元拆合身份变化时保留有效结果，并报告需要显式 remap 接入，不猜身份或改整章。

## 入口与计量

正式入口：`scripts/upgrade3/outline_strengthening.py --mode on_demand`，先 prepare，再按原 CLI 提供账本、密钥文件和 `--run`。本地受控驱动用于额外限制用户本轮预算，不能只依赖共享账本总额度。

准备和客户端复用 `build_qwen_wire_body`、`estimate_prompt_tokens`；模型、流式、JSON、思考与回答参数以真实 wire 记录为准。没有分词器时使用相同 UTF-8 上界。思考与回答各 32,768，未为降本调低。账本按现有原价估值，不预先把缓存折扣当余额。

本轮本地根目录：`F:\OptoMind-Review-2\outputs\on_demand_efficiency_20261007`。原源码、未提交差异、配置与被拦预检在 `BEFORE/`；新驱动为 `run_ch2_efficiency.py`。生产输入来自原第一层选中组，不含人工科学问题、旧加强答案或根智能体评价。

## 验证

专项检查覆盖未引用材料搜索进入负责人、条件/阴性结果/互补与综述转述内容、只读展开、正常调用数量、选材后暂停恢复、实际超容量分批及成功批复用、wire 计量与运输。测试指令：

```text
python -X utf8 -m pytest -q tests/upgrade3/test_outline_on_demand_navigation.py tests/upgrade3/test_outline_strengthening.py tests/upgrade3/test_qwen_effective_token_transport.py tests/upgrade3/test_outline_selection.py tests/upgrade3/test_outline_selection_local_contract.py tests/upgrade3/test_quality_capacity_profiles.py
```

本轮不重新编排或写正文。真实请求与费用、内容亲审另记于本地运行根目录。
