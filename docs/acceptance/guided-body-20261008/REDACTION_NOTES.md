# Redaction and scope notes

Credentials, API keys, bearer/auth headers, signed URLs/signatures, cookies, passwords, secret/key-file paths, and personal email addresses are redacted. Stable stage/cache hashes, block/task IDs, request/call/reservation IDs, wire hashes, and scientific signatures are retained for reproducibility.

The shared FULL_BODY_INPUT is included once and split as canonical UTF-8 payload. Any recognized rights-bound full-source field is replaced with `[OMITTED_RIGHTS_BOUND_FULL_SOURCE_FIELD]`; generated A/B model bodies, actual requests, and project material records remain when available. SQLite, keys, and transport streams stay local.

The actual request audit found zero exact old task/unit/completion-key structures at the author top level, while `outline_action` remained inside material projections. This package records that precise finding and does not make a broader claim that all historical planning fields were absent. Root scientific review is post-run material and was never sent to the models.
