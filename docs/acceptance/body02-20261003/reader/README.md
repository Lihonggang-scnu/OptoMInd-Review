# Reader evidence

Each subdirectory is a sanitized copy of one real directed-reading call. `INPUT.json` keeps paper identity, questions, required outputs, and workflow. `PROMPT.json` keeps the actual task prompt and model-facing contract but replaces the source paper Reading View/fulltext with an omission marker. `DIRECTED_READING.json` keeps the complete parsed generated material while omitting bibliography/reference payloads. `DIRECTED_READING.md` is the generated readable rendering; it is retained as output, not represented as the original paper.

The four records are three distinct P0004 tasks/representations plus one independent P0001 task. The exact repeat is represented by the run configuration and acceptance report; it made zero additional calls.
