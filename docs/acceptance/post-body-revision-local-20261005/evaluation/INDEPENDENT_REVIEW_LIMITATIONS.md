# Independent review limitations

This review is independent issue preparation, but it is not a strict fresh-context blind review: I had previously read portions of the original BODY during earlier issue scouting. I did not read generation run records, decode keys, prompts, sibling judgments, labels, or any other agent assessment, and no new context window or external tool was available.

For this pass I read only `JUDGE_GUIDE.md`, `assessment_schema.json`, and the three specified packet files. I compared both complete packet texts, checked the supplied research question, scope, outline, relevant material records, and source identities, and copied target quotes and packet input hashes from those packets. The judgments are bounded to the three observed diffs and supplied evidence; they do not establish a general ranking or fresh-context scientific ground truth.

## Consistency adjustment

The original `judgments_primary.json` is preserved unchanged. In `judgments_primary_consistent.json`, only `pair-002-0.knowledge_preservation` changed from `preserved` to `loss`. The issue-level regression for the right-side P0388 sentence already had `knowledge_loss: true`; that change replaces the supplied synthetic-antigen mimicry method with an unsupported functional-rescue method and therefore loses useful experimental identity, rather than being a fluency-only difference. The winner remains `left`, and no new blind review or new context was used.
