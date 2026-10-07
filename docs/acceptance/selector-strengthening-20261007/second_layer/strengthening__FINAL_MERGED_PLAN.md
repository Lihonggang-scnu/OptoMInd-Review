# Selector strengthening final merged candidate

这是离线稳定 ID 合并稿，不是正文写作结果。合并基线为 7 章 29 单元的 FULL_CHAPTER_PAYLOADS；只替换下列 3 个已接受 owner 结果，其余单元、章节元数据和材料通道保留。

## 已合并单元

- `Ch1/U1_跨癌种关联图谱`：`updated`；改变顶层字段：argument_relations, ordered_development, paragraph_briefs, substantive_point, supporting_studies；来源 `<LOCAL_PATH_REDACTED>
- `Ch1/U3_动态监测与纵向演变`：`updated`；改变顶层字段：argument_relations, ordered_development, paragraph_briefs, substantive_point, supporting_studies, transition；来源 `<LOCAL_PATH_REDACTED>
- `Ch4/Ch4_U03`：`updated`；改变顶层字段：cases, ordered_development, paragraph_briefs, substantive_point, supporting_studies, synthesis；来源 `<LOCAL_PATH_REDACTED>

## 保留与停点

- 单元数：29 → 29；未选单元：26，逐值保持。
- source/candidate/tool/candidate_navigation 材料通道逐章逐值保持；没有用 accepted_source_materials 替换原材料池。
- Ch2 第三组逻辑组没有付费，仍是容量/预算预检阻塞；没有把它作为失败内容或合并候选。
- 本轮 owner 后不再启动模型调用；质量判断留给 root 的后验审读。

## 机器可核对文件

- 合并 JSON：`<LOCAL_PATH_REDACTED>
- 合并审计：`<LOCAL_PATH_REDACTED>

