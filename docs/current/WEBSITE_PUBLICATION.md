# GitHub Pages 成果入口

地址：https://lihonggang-scnu.github.io/OptoMInd-Review/

网站更新由 `lihonggang-dev` 的 `.github/workflows/pages.yml` 发布。工作流只读检出冻结提交 `e528f7e5503c087a327ed5739ff4d59d40cf45ee` 的 `replay/` 和 `artifacts/e2e/`，保留原有光学回放、PDF及证据文件；`main` 不写入提交。

新增论文登记在 `website/publications.json`。构建器 `scripts/maintenance/build_review_pages.py` 复制清单指定的现有PDF，并在首页原回放区域前追加查看、下载入口。不是把新增论文伪装成旧三组光学回放的某个阶段。

肠道案例使用已发布的手工修订PDF：摘要无引用，作者 `OptoMind-Review`，日期 `2026年8月31日`。网站地址为 `papers/gut-microbiome-ici-review.pdf`，文件内容与验收目录内PDF相同。

相关网站文件变化会自动构建，也可在 Actions 的 Publish Review Replay 选择 `lihonggang-dev` 手动运行。GitHub Pages 环境允许 `main` 和 `lihonggang-dev` 部署；旧main工作流仍保留，正常更新应选择开发分支，避免发布旧页面覆盖新增入口。
