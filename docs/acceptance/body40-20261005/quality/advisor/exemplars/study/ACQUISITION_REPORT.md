# 本地采集交付与验收

考察30篇主样本，27篇真实全文已保存：7篇JATS、7篇BioC、2篇HTML、11篇PDF。每篇合格本地样本都有原件、paper.json、structure.json、READING_VIEW.md；人工结构导读集中在READING_NOTES.md。入口见INDEX.md。

主代理最终独立核验通过：原文件格式及正文非空、原件SHA256与元数据一致、27份阅读视图有效、结构文件齐全、30个主样本DOI唯一。验收结果见ROOT_COLLECTION_AUDIT.json。此项是素材有效性验收，不等于科学结论质量打分。

未取得本地全文的三篇：P03地球系统模型、P06冰盖、P09地质储碳。它们由阅读组查阅线上正文/结构，只列补充阅读；不以元数据、摘要或验证页面代替全文。P06最后的Northumbria和OGS公开仓库请求均403；其余尝试见历史记录。

两个Perspective候选被排除主样本。旧Sex disparities文件仍保留作补充材料；L09现为Beyond genetics。L07为Meta-analysis研究文章，作为量化证据综合对照单独标型；L06为系统综述与贝叶斯Meta-analysis。

下载过程中发现的HTML验证页已撤销成功状态；PDF自动短行目录误识别已改为逐页文本，P04/P05另有人工核对大纲。BioC/HTML的实际来源与格式已独立记录，不能把它们当作原生JATS。部分失败响应保留用于追踪，不计有效原件。

本机Docker启动失败使GROBID不可用。本轮没有拉新镜像、重置Docker或声称生成TEI。PDF原件与页码正文仍可用于结构研究；图表仅作图注与表中文字核验，引用链接与图表层级的自动恢复不具备完整生产保证。

最终主清单是manifest.json；早期采集/失败信息保存在manifest.history.json，早期候选记录保存在candidates.history.json。最终不再使用旧清单中的成功数量。没有付费Qwen调用，没有生产M4/M5改动。
