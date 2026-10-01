# 正文后独立首尾模块：使用与验收

## 使用范围

本模块基于 lihonggang 的现有正文，独立完成一次文章首尾构思及串行生成。它不回调正文 planner，不增加正文规划字段，也不改变章节编排和单元写作。

逻辑调用顺序：

1. `conception`：读取实际正文，生成本篇 `manuscript_parts_plan`。
2. `conclusion`：依据正文建立的认识形成综合与收束。
3. `introduction`：建立阅读前提、问题、范围与组织理由；不能用缩略结语替代入口。
4. `abstract`：生成自含摘要、关键词和题名，核对实际范围与证据强度。

每一步都读取当前正文。此前生成的部件仅供一致性与衔接参考；不能凌驾于正文和真实材料之上。任何步骤失败都停止，不装配部分完成的稿件供后续出版。

## 输入选择

必需：已完成正文的 Markdown、用户研究问题。

可选 `context.json`：

```json
{
  "shared_scope": {"included": "本次实际覆盖范围", "excluded": "本次实际排除范围"},
  "review_argument": "已有组织视角，须由实际正文校准",
  "final_outline": [
    {"chapter_id": "C1", "title": "正文知识问题", "thesis": "正文实际建立的主张"}
  ],
  "material_records": [],
  "source_identity_map": {}
}
```

这里提供的是最终细纲中的必要主张和分工，不是整份历史 PLAN/cache。不要向输入传入旧 `manuscript_parts_plan`。本篇职责卡必须由后置构思重新产生。

默认不整池重喂 B 卡，不自动打开 `card_path`，不自动检索。需要定位背景、已有综述或某项主张时，显式加入相关材料及来源身份。背景材料不能成为摘要或结语凭空增加正文未建立结论的理由。源文件、上下文、模型消息和正文均可能包含未核实内容；不得因存在引用编号就认为科学正确。

## 独立 CLI（离线）

在仓库已配置 Python 依赖的环境中运行：

```bash
python scripts/upgrade3/manuscript_parts.py \
  --draft BODY.md \
  --research-question "本篇综述的研究问题" \
  --context context.json \
  --fixture parts_fixture.json \
  --output outputs/post_body_parts_run01
```

`--context` 可省略。`--fixture` 与 `--recordings` 二选一。输出目录必须是新目录；失败后用新目录重新运行，不把旧成功文件当本轮成果。

fixture 顶层为 `conception`、`conclusion`、`introduction`、`abstract`，分别提供对应阶段的响应。conception 返回 `{"manuscript_parts_plan": {...}}`；abstract 返回 `title`、`abstract`、`keywords`。fixture 是人为离线输入，不是模型质量证据。recordings 的键为 `serial_parts:conception` 等；每条 recording 必须带与本阶段实际 messages 完全匹配的 messages_sha256；缺失或不匹配会停止。未绑定消息的历史响应只能显式作为人工 fixture 做离线试验，不能称为新模型调用。

## 接入现有 delivery

保持原配置 schema。仅显式选择新模式才启用：

```json
{
  "schema": "review_v2_delivery.config.v1",
  "research_question": "本篇综述的研究问题",
  "front_back": {
    "mode": "post_body",
    "fixture": "parts_fixture.json"
  },
  "post_body_context": {"path": "context.json"}
}
```

这是接线片段；完整 delivery 仍需原有正文装配与编辑配置。独立 CLI 不要求重跑它们。省略 mode 时维持 legacy 行为，不自动切换历史流程。

## 后续真实调用

运行库 `run_serial_parts` 提供显式 `provider(stage, messages)` 注入接口。后续本地 Agent 可将已有预算管理与模型客户端接在这里；四个阶段都必须走同一运行库的真实消息构造和解析，不能手工拼接近似提示词另证链路。

本次 CLI 没有隐式实时客户端，不扫描凭证，不替用户批准预算。provider 的计费、重试和供应商请求数由外部受控驱动负责；报告的逻辑 provider 调用数不冒充供应商审计账单。必须先取得真实测试授权；不要从归档的历史剩余预算推导新授权。

## 装配安全与 v1 限制

- 仅 standalone 自动装配；embedded/distributed 可出现在卡片中，但停止并报告不支持，不偷偷转换。
- 正文使用 H2 及更深标题；可有一个位于文首的 H1 文章题名。存在不明确的 H1 章节结构时停止，避免误覆盖正文标题。
- 仅替换本模块拥有的 manuscript-part 标记区域以及已确认的文首题名。明确未标记的摘要、引言、结语位置冲突会阻止生成/出版；不删除该位置的原有深度正文。
- 不支持仅凭细纲定稿。空文本、明显只有标题/目录/参考文献的输入会失败；这是结构检查，不声称程序能证明正文已完整写好。细纲可以作为正文旁的辅助输入。
- 正文上限 400,000 字符；显式材料序列化上限 100,000 字符；筛选后的上下文上限 150,000 字符；单次提示词总上限 650,000 字符。超限报错，不静默截断；未实现分章压缩和自动取材。这是工程字符边界，不是模型 token 容量保证，真实驱动仍需按所选模型核算 token 和预算。
- 保存职责卡、实际消息、响应和运行报告，便于人工检查。离线测试通过不等于首尾质量通过。

## 本地真实验收建议

先固定一份已认可的 BODY，再只运行后置模块。核对引言是否提出值得阅读的问题并交代已有认识，摘要是否压缩实际综合认识，结语是否形成有条件的整体判断；检查是否被少数缺口主导、是否改变术语/因果强度、是否无依据宣称首次或系统综述，以及正文是否保持不变。无需为此重跑正文。

## 写作指导的研究依据

本实现复用前期 35 篇跨领域综述的结构研究结论，研究深度为首尾、主要章节推进与关键图表职责的结构性阅读，不是逐行事实审计。它支持“职责与信息关系”的区分，不证明某种软件调用顺序必然更优。

- [Taking the Human Out of the Loop: A Review of Bayesian Optimization](https://doi.org/10.1109/JPROC.2015.2494218)：引言含数学与教学，扩展/开放问题章可承担深度分析。因此提示词不硬禁引言中的概念、机制或案例，也不以标题决定删除正文。
- [Powder diffraction](https://doi.org/10.1038/s43586-021-00074-7)：方法 Primer 的实质概念入口与 Outlook 展现文体差异，不能强制所有文章使用同一背景—结果—展望模板。前期研究使用作者托管版本，其最终出版版本差异未逐项核验。
- [Roadmap on transformation optics](https://doi.org/10.1088/2040-8986/aab976)：分布式收束说明 standalone 只是本实现 v1 的执行边界，不是综述写作的普遍要求。

本轮测试归档另提供反例：v1/v2 职责卡内容未实际改变，少数缺口被提前设定为首尾核心立场。新模块在 BODY 后重新构思，但仍需要真实内容测试，不能把“换了调用顺序”本身当作改善证明。没有将该微生物组样本的研究结论、病例或论文名写进生产提示词。
