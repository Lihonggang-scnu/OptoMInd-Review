# 编排来源fallback修复验收
根智能体阅读两生产文件的实际diff及四项测试。新增只补当前章节已引用handle的缺失身份/卡路径，不覆盖局部有效材料；显式不同paper_id/DOI或无身份的不同title不会混接卡。未更改planner、提示词、原计划或测试稿。
Luna验证4个聚焦用例和相关arrangement/writer套件合计45passed，compile/diff-check通过，七章零调用preview通过；根另走生产build_chapter_view→build_source_catalog→writer _material_entry真实读盘路径，P0367正确读到22名患者/7健康对照的培养组学研究A、B，issues=[]，没有伪造内容（ROOT_FALLBACK_MATERIAL_CHECK.json）。
七章preview无unresolvable handle，预留估价5.470888元，context约49780至62642。允许同一budget.sqlite真实七章编排，阶段cap6元，总cap40元不变；沿用qwen3.5-plus思考、输出20000/thinking8192、timeout900、no-retry、planning-revision。启动后记录实际PID；结束立即停下根亲审，不自动进入写作。ledger当前实际19.5310296/历史held0.228144不变。
