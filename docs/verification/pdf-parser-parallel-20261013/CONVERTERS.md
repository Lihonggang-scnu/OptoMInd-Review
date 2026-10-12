# 本地科学 PDF 转换器实测

日期：2026-10-13。全部材料来自同一 `FRESH_LOCAL_MANIFEST.json`，共 5 份、78 页，源 SHA256 已在运行后重验，全部未改动。本轮没有调用推理 API、没有加载新大模型、没有动生产源码、密钥、账本、Docker、全局 PATH 或现有环境。全部安装和输出在当前 `converter_trials` 目录。目标是科学内容是否可用；表/图/公式个数不是质量分数。

结论：**保留当前 Docling 主路径，OpenDataLoader base 作为后续零推理费的有边框表格结构候选。暂不接入第三条生产解析器。** ODL 在 bandwidth 的密集表和两份 SI 表格中保留了清楚的行列、单位、标记；但它把 visible_review 的 7 列表压成 1 列，误把 THz 图框识别为表，不能完整导出 bandwidth 的矢量科研图，复杂公式也不可直接用。PyMuPDF4LLM 旧版能生成部分矢量图截图，但密集表和公式结构不足。**没有运行 ODL hybrid，本文不评价 hybrid 的质量或速度。**

## 软件与隔离

- 基础 Python：`C:\Anaconda\python.exe`，3.12.4，仅用于创建本目录 `venv`。ODL 2.5.12；PyMuPDF 1.28.2。
- 首装 PyMuPDF4LLM 1.28.2 会引入 pymupdf-layout 1.28.2 和 ONNX Runtime 1.31.0。导入前还没有加载 PDF 就发生 Windows native access violation，退出码 `3221225477`（PowerShell 显示 `-1073741819`）。复现见 `pymupdf4llm_default_import_failure.json`、`import_verbose.log`。卸载 layout 并不能排除错误：该版本 OCR 模块仍在 import 时导入 ONNX。
- 限量兼容尝试：ONNX Runtime 降到 1.23.2 后改为 DLL initialization ImportError；5 份任务分别失败，完整 traceback 在 `pymupdf4llm_latest/<label>/stderr.log`。这不是 PDF 内容质量结论，而是本机依赖稳定性失败。没有无限修依赖。
- 最终轻量基线：PyMuPDF4LLM 0.0.27（pip `--no-deps`），配 PyMuPDF 1.28.2，未用 layout/ONNX/OCR。全部 5 份转换成功。最终版本快照 `requirements-freeze-final.txt`。
- 限定常见 Program Files Java 目录未找到 java.exe。Adoptium 官方 API 返回 403，已保留 `bootstrap.json`。改从 Adoptium 官方 GitHub release 下载 portable Temurin **JRE17.0.20.1+1**，下载 6.888 秒；官方 sha256 文本与实测 SHA256 一致：`bc21a93923103cdaac93ee337b0ae4365e739fde36df823dd456bc67c8a9d352`。见 `java_assets.json`、`java_setup.json`、`java-official-sha256.txt`、`java-version.log`。只向转换子进程 PATH/JAVA_HOME 加入该目录。
- venv 创建 3.678 秒，首批 pip 安装 31.963 秒；安装/转换有 600 秒上限。未出现超时。

## 实际耗时与输出

表中为逐份独立进程 wall time（秒，包含启动；单次样本），不是纯引擎时间。ODL 导出 Markdown（允许 HTML 表）、原生 JSON、原生 plain text、HTML、外部图像；0.0.27 导出 page_chunks JSON、Markdown、图像，`document.txt` 是拼接的同一 Markdown 内容，并非另一种纯文本算法。`source_review/<label>.txt` 是原 PDF text-layer 参考，不能当作转换器成果。各运行完整命令、文件大小和状态见对应 `*_manifest.json`；纯转换计时见每目录 `timing.json`。

| PDF | 页数 | 既有 Docling 进程秒 | ODL base | ODL cluster | PyMuPDF4LLM 0.0.27 |
|---|---:|---:|---:|---:|---:|
| bandwidth | 8 | 14.797 | 1.390 | 1.453 | 2.663 |
| thz_review | 25 | 11.094 | 12.371 | 11.939 | 12.423 |
| visible_review | 17 | 7.469 | 1.459 | 1.499 | 1.876 |
| multilayer_si | 20 | 7.469 | 1.604 | 1.673 | 1.443 |
| broadband_si | 8 | 17.312 | 0.956 | 0.906 | 0.731 |
| 合计 | 78 | 58.141 | 17.780 | 17.470 | 19.136 |

ODL base 与 cluster、旧版 PyMuPDF4LLM 均 5/5 exit 0。PyMuPDF4LLM 最新版在五份任务中均启动失败（每份约 0.8 秒），没有成果。THz 对两个成功转换器都明显较慢，伴随大量图像处理。Docling 是此前同批结果，非本轮同时重跑；机器缓存、并行负载和输出内容不同，不能把上述比值泛化为严格性能排名。ODL 官方建议批量一次 JVM 可省启动开销，本轮为逐份可归因而独立启动，未把批量优化当实测收益。

## 关键原页的人工对照

原页已渲染为 `source_review/*_p*.png` 并看图；相关转换 Markdown 摘页和 `docling_comparison.json` 保留，所有原始转换 JSON 未覆盖。抽查可用于发现实质损失，不宣称全书字符级正确率。

### bandwidth：p8 Table 1 与 p3 Eq.(6)–(12)

原表 11 列、28 数据行加表头。ODL base 的 HTML/JSON 每行分开；抽核 a1 行 `63 / 470 / 670 / 600 / 6.5 / 1200 / 462 / 2500 (12500) / 2440 (12200)`、h2 的 SR `36.1`、n 行 `F=12000 µm`、`λmin=0.375 mm`、`λmax=1 mm`、`L=550 µm`、Miller `5.96 (29.8)`、Tucker `4.64 (23.2)`均保留，并保留表注说明括号为 `ΔTerr=0.8ΔT` 的放宽界限。列头 `F / µm`、λ、ε、Δω 优于既有 Docling 字符映射（Docling 是 `F / mm`、`lmin`、`#rmax`、`Dw`，会造成焦距单位误读）。Docling 行结构也完整，故 ODL 的优势在这个实例的字符/单位，不是“Docling 无表”。

PyMuPDF4LLM 0.0.27 将 28 行压成一条数据行，每列内 28 值用 `<br>` 堆叠。值大多还在，但失去逐行记录；末行厚度呈 `550_ µ_m`。仅有表计数仍会报告成功，实际不适合自动字段入库。

ODL p3 Eq.(6) 根号完全消失，分子/分母拆成多段；Eq.(8) 的 `L/λc` 变成上下分离字符串，Eq.(9) 的 π/√3/L/λc/ηmax 顺序重排；文字中的 `L ≫ λc` 只剩 `L λc`，`εmax ≳ 6` 只剩 `εmax 6`。简单 `κ=2`、式号仍在。PyMuPDF4LLM 同页出现 `�` 根号和分数、`[2]` 伪脚注/指数、乘除结构散落；它保留了文字中的 `≫` 等少数符号，但不能作为可执行数学或可信 LaTeX。两者都没有结构化 LaTeX 公式输出。对照现有 Docling JSON，其 formula 条目的 `text` 也为空，`orig` 只是尚有损坏的原文字层；因此本轮也没有证据称 Docling 已完成公式转写。`docling_comparison.json` 保留这些 `text/orig`，broadband_si 甚至把 p7 参考文献片段标成 formula，进一步说明公式计数不等于质量。

### multilayer_si：p15 Table S2

原表六列、五数据行。ODL 保留表头 `Aperture (µm)` 与 `Bandwidth (nm)`，Chen 行 `1500 / 470-700 / 0.075 / TiO2 + Silica / 35%`、Huang 行 `5/8.6 / 470-650 / 0.61 / TiO2 / 50%`及 Mclung `N.A.` 原样保留；不能擅自把原表 `5/8.6` 自动解释成两项新数值。表后“效率默认是波段平均，注明波长者例外”文字也在。既有 Docling 在该表的行列和值同样正确。

PyMuPDF4LLM 值基本正确，但误把表标题横向切成六个表头格 `Table S2. Summary ... | of multi-layer a | achromatic metale ...`，真正六个字段名成为第二行。若自动读取第一行为 header，会直接错配。两者表中 TiO₂ 变为 plain `TiO2`，化学下标样式未保留。SI 其余数学公式没有可验证的结构化转写；语义公式检测数量不能补偿分式/重复字形损失。

### broadband_si：p1 Table S1 的效率分母

ODL 保留七列、十三数据行及最重要的表前解释：`*` 是 focal-region power / incident power；`+` 是 focal-region power / transmitted power through metalens area。逐行 `25%+`、`~7.9%+`、末行 `~20%+` 与 `~65.6%(average)*` 保留；5.2 µm 与其余 nm 波长、FOV `>170°`、`≈180°`、NA 0.80 都保持，末行 `This work` 未裂开。既有 Docling 同样保留 * 与 +，但原表中表头含断词 `Focusing effi- ciency`。

PyMuPDF4LLM 也保留分母说明和行标记，所以这张表没有理由判作丢失分母；其表头有 `Norm<br>.<br>relative<br>focalshift`、`Focusing effi<br>-<br>ciency`、`Ref<br>.`，末行 `This<br>work`。问题主要是结构清理负担，非数值缺失。数字字段必须把标记连同该表注保存，不能混算 incident/transmitted 两种效率。

### visible_review：p6 Fig.3 下方七列表

原表字段 Reference / Efficiency / mode / material / λ / polarization / t/λ，七条文献行。ODL base 与 cluster 都仅给 2×1 表，把整行表头合为一个格，各列值纵向串在同一数据格；例如 b 行本该配 `80% / R / Ag / 10.6 µm / linear / 0.7`，输出没有独立行列来保证这个关联。Docling 的同表为 8×7，结构明显更好。ODL cluster 未修复此问题。

PyMuPDF4LLM 0.0.27 在 p6 没有输出这张表的文本，主要内容变为两个图片链接；表值只能从截图读，无法直接入库。ODL p11 图/文字混合对比表结构也有合并行/列，需要保留原图复核，不能因“2 tables”视作表格通过。

### thz_review：p3 方程与 p14 Fig.10

ODL p3 能保留正文和广义 Snell 定律相关词，但 `λ/(2π) dΦ/dx` 拆成 `dΦ dx`、`λ 2π` 和方程主体，乘除关系不能靠段落顺序恢复为可靠公式。两个成功转换器均无可验证 LaTeX 输出。

原 p14 是多子图 Fig.10，不是数据表；ODL 将右上四个曲线面板识别成 2×1 table，将坐标/图例 `δx=0/10/20`、`Clockwise` 混入单元格。cluster 更额外把 p1/p13 的布局框识别成表，没有消除这类假表。179 image 节点含曲线图例线和小图素材，不是 179 个完整科研图。既有 Docling 同文 0 表、15 picture 区域与原页语义更一致；数量本身仍不是质量认证。

## 图像结论与重试

ODL bandwidth 导出六个 312-byte、128×128、相同 SHA 的 PNG，最初视觉像空白图。**进一步与原 PDF image-info/bbox 对照后，确认它们是 Fig.1 内约 15×15pt 的原始透明黑色小素材（标记/省略符），不能称为 ODL 自造占位图。** 原 p2 Fig.1 是大量文字/矢量与少量嵌入素材的组合；p5 Fig.2/Fig.3、p6 Fig.4 也以矢量绘制为主。ODL 没有导出这些完整科研图，JSON 给出的图 bbox 只是小素材 bbox。

隔离诊断：对 bandwidth p2/p5 显式 `image_output=external,image_resolution=144.0,content_safety_off=all` 重跑 PNG，仍仅六个小素材；JPEG 模式没有写出图文件。见 `odl_image_retry_png`、`odl_image_retry` 及日志/设置。不能把故障归因于安全过滤，也不能认为提高分辨率即可恢复完整矢量图。官方当前 ImagesUtils 实现源码参考保存在 `ImagesUtils.java.source.txt`（main，非生产修改）。

ODL 其他四份确实导出真实数百 KB 图像，例如 visible_review 中 Fig.3 合成图；但 thz_review 易碎片化、broadband_si 只导出部分嵌入图，科学图像完整性需逐图核验。PyMuPDF4LLM 旧版能把 bandwidth p5 曲线矢量截图保存成可视 PNG，已看图；其 Fig.3 截图漏外侧坐标轴标题/单位，文本另行散落，故也不能当完整可引用图。它 p2 Fig.1 仍被切为小片，不是完整复合图。建议继续用已有 Docling picture bbox + 原 PDF 高分辨率裁图作为证据图，而不是按嵌入 asset 个数验收。

## 自动化与后续建议

1. 主流程保留 Docling。ODL base 可以试作有边框表的并行候选，尤其用于交叉检验 Unicode/单位；先用明确字段/原图验证，再考虑接入。本轮不接生产。
2. 要保留 ODL 原生 JSON 的 page number、bounding box、table row/cell/row-span/column-span、content，适合后续和 Docling region 对齐；但应拒绝“图框假表”以及只有单列的多列表。bbox 不能保证语义正确。
3. 表格质量验收至少覆盖逐行对应、表头单位、跨单位行、括号双值解释、效率 * / + 分母。图像需要完整图+坐标单位；公式需要 fraction/root/subscript 结构，不用计数充当质量。
4. PyMuPDF4LLM 1.28.2 本机依赖未稳定，不推荐替换；0.0.27 仅作为轻量截图/全文候选，不能作为公式和密集表权威。其 AGPL/商业许可应在未来产品化时按项目场景核查，本轮仅本地评估。
5. Hybrid 尚未运行。官方把复杂表、OCR、LaTeX 放在 hybrid 能力中；本轮 base 的不足不能推出 hybrid 也不足。未来如确需评估，可复用已有 Docling 环境/模型设计独立实验，不为本轮重复部署。

证据入口：`TRIAL_SUMMARY.json`（源 hash、版本、各运行清单）、四个 `*_manifest.json`、`structure_inventory.json`（原始表/图节点，统计仅诊断）、`docling_comparison.json`、`source_review` 原页渲染与对应摘页、三个成功模式目录内全部全文/JSON/图像、最新版失败日志、安装日志、官方 Java 校验记录。实验目录体积包括安装包/venv，不是解析产物体积。

官方资料（本轮读取）：
- OpenDataLoader Python 快速入门：https://opendataloader.org/docs/quick-start-python
- OpenDataLoader CLI 选项：https://opendataloader.org/docs/reference/cli-options
- OpenDataLoader 官方仓库/能力矩阵：https://github.com/opendataloader-project/opendataloader-pdf
- PyMuPDF4LLM API：https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/api.html
- Temurin 官方发布源：见 `java_setup.json` 的 download source 和 `java_assets.json`。

