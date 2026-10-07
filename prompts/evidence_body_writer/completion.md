当前稿件正常结束但仍有明确缺失任务或必需成品表格。保留 existing_scope_body_markdown 的每个字符，只补当前 missing_task_ids 所需的新文字或实质表格；不要重写整章，也不要为每项任务制造一张表。可以用一个足够的表共同完成多个任务。
输出 JSON：{"insertions":[{"after_anchor":"原稿中恰好出现一次的完整原文锚点，或留空表示末尾追加","text":"新增完整文本，包含所需换行"}],"completed_task_ids":["本次实际补全的 missing task ID"],"complete":true}。只允许插入，不允许删除、替换、改写前文。系统将在原文上应用插入并重验完整当前章；无法有效完成时保留稿件并说明问题，不伪报完成。
