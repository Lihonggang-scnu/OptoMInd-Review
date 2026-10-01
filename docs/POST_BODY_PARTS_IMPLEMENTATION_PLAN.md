# lihonggang：正文完成后的独立首尾模块

## 基线与边界

- 实现基线：`e100e2066028bd60f56796e3b31849293bca1536`（lihonggang）。
- 实现分支：`lihonggang-serial-parts`。不改动现有 main、review-v2、review-v2-manuscript-parts 或 lihonggang。
- 参考归档：`516af116f88356d0e4d82a80f8bd1871bc47ff7e`。实际最终测试代码为 `32d7349158283eb52bdcc232a38c28fc700412f0`，不是所有历史调用均可追溯到这一 SHA。
- 不修改 progressive_review_plan、chapter_arrangement、review_unit_writer、材料路由或正文装配算法。不导入实验分支的全局规划改造。

## 输入输出

已完成的正文 + 用户问题与范围 + 最终细纲中的主张/分工 + 可选的少量真实材料

→ post-body conception：根据实际正文生成本篇 manuscript_parts_plan

→ Conclusion → Introduction → Abstract / Keywords / Title

→ 安全装配，正文保持不变

首尾职责卡在这一流程内新生成；不能直接采用历史并行卡片。共用研究规律约束知识职责，不强制某一学科的框架。每次生成继续读取同一份实际正文；先生成的首尾是衔接参考，不成为新增科学事实的权威。不全池重喂 B 卡，不新增来源路由，不自动检索。

## 最小接线

- 新独立运行模块、严格轻量职责卡验证及独立安全装配助手。
- 新独立 CLI 可消费已经写好的正文，不要求重跑规划或编辑。
- review_delivery 仅在显式 `front_back.mode=post_body` 时走新模块；原有入口保持兼容。
- 默认支持离线 fixtures / recordings；通过显式注入 provider 供后续本地预算受控调用。开发不构造实时客户端、不调用付费模型。
- 自动装配 v1 只支持 standalone；其他 placement 保存并报告不支持，不能静默转换。
- 未归属 marker 的明确首尾位置冲突、无效响应、缺阶段、超限输入均阻止出版，不删正文解决冲突。

## 离线验收

验证四阶段真实消息依赖、早期卡片拒用、材料边界/身份冲突、语言、空输入和超限、失败短路、marker 幂等、明确位置冲突及正文逐字保留。用归档真实 BODY 做离线装配，不把历史响应重放称为新模型质量验证。比较全部 upgrade3 失败集合与基线；检查受保护正文代码无差异。

## 真实测试边界

离线通过只证明合同、传递和装配可运行。职责卡与文字的实际质量、背景补充需要和额外构思调用的成本，须由后续经用户授权的本地真实测试判断。旧稿确切生成提交未知，不能把旧稿质量等同于某个已证明的代码版本。
