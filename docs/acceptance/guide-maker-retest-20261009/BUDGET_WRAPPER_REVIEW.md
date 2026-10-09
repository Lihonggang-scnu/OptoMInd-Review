# 本轮预算包装亲核记录

这是本地测试启动器的核对记录，不是生产模块修改。用户只授权在原30元账本上新增最多10元；所有请求都受这一增量边界限制。

固定S0=0.517030，cap=10.517030，原数据库limit=30。S0写在本轮固定根目录，输出路径变更不会新建起点；已有S0恢复按稳定账本身份校验，不用当前增加后的费用重算。没有更改原账本标记或上限，没有移除费用。

付费前根智能体阅读初版启动器，要求修正三处：

1. 工作区目录不能按文件存在性检测。
2. 恢复时不能把S0与当前费用相等作为条件，否则已付费后会拒绝恢复。
3. 正式factory从module4.runtime动态导入类，只改包重导出会漏拦；必须在权威runtime及已加载aliases处包装，正式factory产出的客户端须携带受限ledger。

这些修正在真实调用前完成。原子reserve仍调用已有GlobalBudgetLedger实现；初始化先校验原30元，之后实例上限收紧到本轮cap，每次refresh/reserve保持限制。PlusOnlyClient同时拒绝构造与调用覆盖中的非Plus模型，并要求受限ledger、零重试和单密钥。运行期间锁住原账本旁本轮入口锁；开始前没有发现其他使用该账本的进程。

免费控制七项通过，见free_tests/SELFTEST_RESULT.json，包括正式factory、网络边界、实际费用增加的模拟snapshot不改变S0。生产模块专项另有90通过/1项Windows临时文件占用失败，不能混写成全过。包内WRAPPER_EXCEPTION.json属于准备过程中的免费异常，不是后续真实生成失败。

根智能体还核对实际首请求：Plus、enable_thinking=true、thinking_budget=16384、max_completion_tokens=49152、stream=true。预占2.169732元；唯一真实调用最终结算0.499740元；账本累计1.016770，reserved/uncertain=0，原marker不变。见cold_start_live/BUDGET_CAP_CHECK.json与stages中的实际参数/用量。

最终wrapper源码与哈希随CODE_PROVENANCE.json提供。初版源码没有完整版本快照；以上三处来自付费前实际阅读及修正消息，不能冒充三份可重放版本。未再启动第二轮冷启动，没有用10元剩余额度重复试到满意。
