# 两项实施本地验收入口（2026-10-12）

云端ZIP已原样保留并通过bundle导入；本地通用修复、免费回归、真实首尾/校验及下游编号/Markdown出版均完成。

优先阅读：

1. [ROOT_REVIEW.md](ROOT_REVIEW.md)：总体质量、修复、版本、费用、运行入口。
2. [ROOT_PARTS_REVIEW.md](ROOT_PARTS_REVIEW.md)：首尾逐项亲读与BODY对照。
3. [FINAL_ACCEPTANCE.json](FINAL_ACCEPTANCE.json)：选用稿消费、来源身份、真实费用与原始返回索引。
4. [CACHE_RESUME_EVIDENCE.json](CACHE_RESUME_EVIDENCE.json)：正式同目录恢复无新增调用。
5. [MANIFEST.json](MANIFEST.json)：核心产物及SHA-256。

源码HEAD：b98771f2cc1548c301f6621d0b87414d858d9f43（本地已提交，未推送）。9次真实调用共1.4544032元，未结算0，20元剩余18.5455968元。

结论：精确局部编辑和实际选用稿传递有效；首尾新增引用，但目录化、重复及结果混并仍需改善。未重跑BODY，未人工改模型稿，未启用PDF编译。

历史真实运行留在live_checker/；修复版最终运行在live_checker_after_seam_fix/；真实执行报告副本在final_live_snapshot/，避免把后续免费恢复计数当作历史调用数。原始产物与人工亲审分开。
