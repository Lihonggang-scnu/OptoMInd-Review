核对本章交付状态。判断范围为 chapter_assignment；manuscript_guide 用于理解章节分工。draft_body_markdown 是已保存正文，prior_metadata 提供已有声明与缺口。

判断要求的解释、比较和图表是否已落实，接受作者合理的组织、标题和表述变化。只将影响本章主要内容或明确要求图表的实际遗漏列为缺口。有效的未完成声明及已有缺口继续保留。

本次只返回状态，原正文逐字保留。JSON 格式：
{"complete":true,"remaining_content":[]}

存在实际缺口时 complete 为 false，remaining_content 用简短自然语言指出具体待补内容，例如“尚需给出两种方法的比较表”。缺口补齐与科学正确性分别评价；本次处理交付状态。
