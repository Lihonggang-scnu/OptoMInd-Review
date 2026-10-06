# 两组实际请求的根智能体验收

锁定源码：4d81a772bdecc4da18a84a8bc90298fb1a38f625。

完整材料 Plus：从实际 user message 解码生产载荷，source_materials、tool_materials、chapter_plan、actual_local_body、readonly_neighbor_unit_roles、source_identity_map 六字段与原始 REQUEST.payload 严格相等。不是仅检查磁盘源文件。

按需 Max 选材：根智能体独立检查实际 MATERIAL_CATALOG source_supplied_locators。29个原始 source_materials 根节点中的 A.key_findings 109/109、B.planning_summary 29/29、B.scope_interpretation_cautions 114/114 均逐条完整可见，无截半条记录。这一口径不包括嵌套工具材料，因此不与旧报告116/118直接混算。工具根节点 conditions/limits 未在短展示内完整展开，但均有 available_material_paths，可请求并读取完整原记录。P0478旧/current重复精读内容须去重统计，不能把父对象 excerpt=true 误报为完整子字段被截断。

实际能力配置：Plus完整材料负责人、Plus自主选材、Max按需负责人均 thinking=true，32768思考+32768回答，wire completion capacity=65536。Max最终材料输入尚待自主选材结果，导出后再次亲审。

执行许可：两组首步骤（Plus负责人、Plus选材）可运行；Max负责人须最终请求检查后启动。共享预算仅显式45→85，保留全部历史费用和占额，本轮新增上限40。科学问题、历史加强结果和本地评价不得进入请求。仅两单元，不全量细纲，不写正文。
