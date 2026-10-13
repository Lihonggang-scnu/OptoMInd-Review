# A/B无人值守恢复与光学池放行（2026-10-13）

生产源码 e8dbc42f0ab474376540cd7ac9360131c2a17678。历史32并发调用源码/费用见同级 ab-concurrency-20261013。恢复是基础能力，科学内容不由程序填充。

## 本轮修复

- 正式paper_reading_batch默认先免费验证已保存的完整RAW_RESPONSE，不依赖调用者记得恢复开关。验证失败保留错误，不落低质量固定字段，也不买重复回答。
- 明确的regional_dispatch_stopped_before_reservation可有界重试一次；未知/在途费用仍保留，不据超时假定免费。
- 同一output-root的run及会写状态的preflight都持有进程级文件锁，重复进程明确返回batch_already_running；进程退出由OS释放锁。HTTP请求仍32并发。
- 光学桥接器兼容历史读取投影：校验COMMIT全部文件、身份与topic，并用当前原始manifest/blocks/assets/references加原保存reading_packet重算原snapshot hash。真实材料或参考文献更改仍拒绝，旧卡不改写。

## 验证和质量

54项有界恢复/锁控制通过，含跨进程冲突与预览同根拒绝。真实244张卡交接全部通过，其中5张历史投影通过上述证明；改变参考文献的反例仍拒绝，零模型调用。

244卡内容全量扫描无空卡、占位模板、整卡重复及身份串用；独立亲读9张完整A/B，5份完整输入，另核对关键源段。root另亲读两张理论卡。允许的局部模拟/实测归属、FWHM含义等错误单独记录，未将评价或科学答案输入后续生成。

剩余7份历史failed与19份无可读材料保持原状态；规划只消费已验证卡，失败缺口必须显式绑定并记录，不能改成成功或metadata。独立验收JSON与root放行记录附于本目录。

## 使用

正式入口仍 scripts/upgrade3/paper_reading_batch.py；保留同目录、同manifest、同材料、同预算及模型配置恢复。--reparse-saved-responses保留旧调用兼容，免费重校验现已默认执行。成功产物保持缓存复用，既有账本不能清零。

本地光学运行根目录 F:/OptoMind-Review-2/outputs/e2e_optics_20261012_80cny；preparation/ab_unattended_20261013保存逐项证据和下游预览。开发期人工supervision不是产品默认运行前提：审读应使用正式decide接口，不手拼receipt，也不因生产已支持的无损别名误拦。


## 正式保存响应恢复补充

字段兼容使用原文：key_findings内明确的warning/result/observation等内容字段可作为finding别名；冲突仍拒绝，conditions-only不填充。单个显式[finding]标记可无损拆分条件与发现。格式恢复与真实内容质量分开。

正式run_paper_reading_card(saved_response_dir=...)也验证历史投影兼容，严格保留原输入、prompt、runtime和来源。72项相关检查通过。两份真实历史RAW已通过该正式入口免费恢复（一个projection_compatible=true、一个false），原RAW哈希不变，无客户端/账本创建；证据见REAL_SAVED_RECOVERY_CHECK.json。

规划已使用锁定244卡池开始运行。以上两份新恢复派生产物未悄悄改入正在运行的输入，批状态和显式排除回执保持启动时一致，后续按正式恢复合入。
