你在补充当前章的实际内容缺口。manuscript_guide 给出全文写法，chapter_assignment 含本章安排、draft_body_markdown 原稿和 remaining_content 自然语言缺口，materials 为来源内容，accepted_body_markdown 为本章之前的真实正文。

补上能够解决这些缺口的解释、比较或必要图表，并使新增内容接得住原稿。保持已有有用内容，不把整章重写一次，也不转换成逐条细纲任务答卷。不要机械重复已经讲清的背景或补充一般性免责声明。

新增正文以现象、对象及关系作主语，来源编号只用于引用。引用使用材料中已知精确句柄的半角方括号形式 [P####]，不发明或重新编号来源。自然承接实际原稿，不重复章间预告。

返回 JSON：
{"insertions":[{"after_anchor":"原稿中唯一出现的原文片段","text":"在该片段之后插入的实际正文"}],"complete":true,"remaining_content":[]}

after_anchor 为空字符串表示追加在本章末尾。锚点来自本章 draft_body_markdown，不来自前章。用最短但足够唯一的原文定位；不虚构原文，不删除或替换已有内容。允许多个插入位置，但不要重复插入相同内容。

如果给定材料仍无法完成某项内容，保留已经能写出的补充，complete 为 false，在 remaining_content 中写清剩余的具体缺口。不要报告任务 ID、评分或思考过程。
