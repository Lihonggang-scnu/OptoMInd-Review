# PDF自动解析与低价候选实测：根智能体结论

## 采用决定

保留 **原生XML优先；PDF用本地Docling定位/裁图，Plus定点转写公式和表格** 的已接通路径。它现在能从真实PDF自动执行到可供阅读的不可变材料快照，成功响应可免费恢复，图像与出处随材料保留。GROBID保持可选，不依赖本机Docker恢复才能运行。

更便宜的模型确实值得试，本轮已经真实测试专用OCR，并实跑OpenDataLoader。结果是：便宜模型在个别表格上很好，但扩大样本后没有达到足以默认替换Plus的稳定质量；本地转换工具也各有缺口。保留候选与原始结果，不为了节省几分钱换掉更稳妥的基线。

光学E2E仍暂停；没有继续A/B、规划或正文，没有提交/推送Git，没有修改F:/OptoMind那份独立工作区。

## 自动链与真实范围

- 同一批5份PDF、78页：理论文章、两篇综述、两份补充材料，来源哈希见FRESH_LOCAL_MANIFEST.json。不是跨领域泛化成绩。
- Docling全页本地解析完成，自动检测34个公式候选、6表、46picture区域；一个公式候选实为参考文献。图片以原PDF的坐标裁图保存，不让模型推断科研图结论。
- Plus真实40次区域请求，0.0678088元。最后免费重解析后35个recognized、4个uncertain、1个空公式缺口。recognized只是接口及结构状态，不能当科学正确率。
- 两处模型省略label/uncertainty保留不确定；一张表返回空columns但首行确为表头，程序逐字提升首行、保留每个单元格并标uncertain；实际LaTeX解码控制字符也标出。没有手改科学公式。
- 最终快照见final_plus_free_replay/FINAL_PLUS_REPLAY.json。正式入口每份跑两遍，10次恢复，NoSendFactory保证0个模型请求；原始PDF、原始付费请求/响应、历史快照均未覆盖。
- 根智能体检查最终快照的standard/economy/concise实际阅读messages，表/公式文本全部送达；practical及legacy directed投影也可读。broadband补充表的incident/transmitted power两种分母在正文输入中保留。见CONSUMER_SEAM_CHECK.json。

## 模型比较：同自动裁图、无人工科学答案

| 模型 | 实际范围 | 已结算元 | 未确定占额元 | 决定 |
|---|---|---:|---:|---|
| qwen3.5-plus | 5PDF，40区域 | 0.0678088 | 0 | 保持基线 |
| qwen3.5-flash | bandwidth，18区域 | 0.0121168 | 0 | 不扩样，复杂公式明显误识别 |
| qwen-vl-ocr-2025-11-20 | 5PDF，39请求，另1区域因前一失败未调 | 0.0129367 | 0.0049445 | 保留低价候选，不默认替换 |
| qwen3.5-ocr | bandwidth，18请求 | 0.0077450 | 0.0130255 | 不扩样 |
| qwen3-vl-flash | 1请求 | 0 | 0 | 当前端点404 model_not_found，未测质量 |

OCR模型走单user消息，不发送它们不支持的thinking/强制JSON参数。完整JSON或完整外层json围栏后严格校验；没有从任意散文猜答案。两种OCR表格本轮显式4096输出，避免假设账号具备更长输出权限。通用模型和Plus默认参数没有降档；模型不静默回退。

根智能体实际对图看到：
- Plus与qwen-vl-ocr都保住bandwidth表28数据行×11列、微米/毫米混合单位、括号中的放宽上界。Plus局部i3行标签漏3；OCR保留，属于OCR真实优势。
- qwen-vl-ocr在bandwidth的复杂Eq12保住根号/Theta/分式，首篇18区域仅0.0055764元，比Plus同图0.0313888元低约82%。
- 扩样后，THz原式`n_t sin(theta_t)-n_i sin(theta_i)`被OCR读成两个相同i下标；Plus该式正确。Jones矩阵上标也有局部符号误读。
- OCR多张表返回不符现有列结构合同或非法JSON；一次流终止不完整。此处区分接口失败与内容错误，未据此否定专用OCR内置任务的潜力。
- qwen3.5-ocr把Eq12的Theta读成odot，另有两条JSON转义失败，密集表自发追加位置字段耗尽4096输出。保留原响应，不自动付费重试。
- Plus仍有一个复杂Jones矩阵起始算符异常/控制字符风险，最终标为uncertain；原图可回看，不宣称所有公式准确。

## 本地转换器

详见converter_trials/CONVERTER_TRIAL_REPORT.md与TRIAL_SUMMARY.json，根智能体已读完整报告并抽查表格原页/转换结果。

- OpenDataLoader2.5.12本地base：5/5转换，共17.780秒；密集有边框表与两份SI表较好，Unicode单位优于Docling原始文字层。但visible七列表被错并为一列，THz曲线图框成假表，完整矢量图未导出。不能只用它代替当前组合。
- OpenDataLoader cluster：5/5，共17.470秒，没有解决上述复杂表，增加假表，不采用该变体。
- PyMuPDF4LLM0.0.27：5/5，共19.136秒；部分截图有价值，28行密集表被并为一行、表题被误当表头，不采用为结构权威。
- PyMuPDF4LLM1.28.2：本机layout/ONNX原生崩溃及DLL导入失败，保存复现，未成功解析，停止依赖折腾。
- 全部安装在converter_trials隔离venv和portableJava。Java官方SHA校验一致；无全局PATH/环境改动。OpenDataLoader hybrid未跑，不能把base不足扩展为hybrid结论。

## 成本与产物

本次多PDF与模型新增已结算0.1006073元，另0.0179700元未确定占额；项目累计已结算0.1297465元（包含此前M1与旧解析试验），reserved=0。未知占额仍保留，不当免费，不改历史账本。COST_SUMMARY.json为只读账本汇总；全轮没有突破已有增量1元限制。

重要入口：
- 生产CLI：scripts/upgrade3/build_local_materials.py；配置与实际命令见root_formal_run.py及published/*/*/receipts。
- 原始PDF与全页解析：FRESH_LOCAL_MANIFEST.json、fresh_local、docling_cache。
- 实际请求/响应/有效参数/读取区域：regional_cache/*/attempt-*。
- 最终采用的Plus快照：final_plus_free_replay/FINAL_PLUS_REPLAY.json。
- 对照：ALL_MODEL_RESULTS.json、OCR_VL_ALL_RESULTS.json、OCR_35_BANDWIDTH_RESULTS.json；raw RESULT与免费REPARSED分别保留。
- 源码：source_checkpoint包含修改前完整diff与全部新增源文件；最终冻结记录另存，基础SHA为37a482cc97a083cdbf98e8ac1a0e52e8ec505354。

官方价格/能力依据（本轮读取）：
https://help.aliyun.com/zh/model-studio/model-pricing
https://help.aliyun.com/zh/model-studio/qwen-vl-ocr
https://help.aliyun.com/zh/model-studio/qwen3-5-ocr
https://github.com/opendataloader-project/opendataloader-pdf
https://opendataloader.org/docs/quick-start-python

下一步最值得做的是在后续新材料上继续观察默认组合的实际缺口；OpenDataLoader可作为零推理费表格候选，专用OCR可改用其原生任务接口另行评估。此次不为追求最低单价扩大模型排列组合，也不恢复光学E2E。

## 最后供材接缝：无损合并重复定位元数据

根智能体检查真实阅读请求时发现，bandwidth表319个单元格各自重复携带解析路径、原文件哈希及全表坐标，表结构序列化达343,877字符。此次只改Docling的模型读取投影：公共审计字段放入显式cell_locator_defaults，逐格定位字段按说明继承；未知字段、数值、行列、跨度、表头和独立定位均保留。原生XML投影保持原形。

独立ROOT_PROJECTION_CHECK.json对五份实际快照的六张表逐字段重建，结果全部与原结构相等；原DOCUMENT_ASSETS.json哈希不变。bandwidth表结构降至103,658字符；economy阅读请求由486,657降至241,682字符，约减50%。没有字符截断，也没有重新调用模型。standard/economy/concise、practical及legacy directed实际输入检查记录见CONSUMER_SEAM_CHECK.json；完整源码另存final_source_checkpoint/MANIFEST.json。

根智能体最终免费回归覆盖区域识别、模型价格/容量、Docling桥接、结构化材料消费者、Qwen实际token参数及双账本，133通过、4个缺少可选本地fixture的测试跳过。另有五PDF实际消费者检查和六表精确重建检查；这些结果与前述检查有重叠，不相加冒充独立测试数量。
