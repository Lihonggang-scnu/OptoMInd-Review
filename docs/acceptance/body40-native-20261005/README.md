# BODY40 native acceptance package

This package records the bounded native Windows acceptance of the BODY40 identity and citation first stop at source commit `e75c66c1ffd018a93636960caa7c51e74911c2af`. It contains the native control logs, boundary evidence, production-consumer replay outputs for the original local Ch6_U4 and Ch7_U02 records, and the 29-unit offline assembly report.

The replay provider used saved responses and made zero live, paid, network, search, or download calls. The result is a consumer replay, not a new scientific generation and not a byte-for-byte reconstruction of the original provider request. The saved-response hashes and original/generated message hashes are recorded in `evidence/native_replay_report.json`.

Raw response files, copied `UNIT_INPUT.json`, `UNIT_MESSAGES.json`, chapter arrangement payloads, request payloads, downloaded papers, full-text caches, credentials, and signed URLs are omitted from this package. Their omission and the fields retained for verification are documented in `SNAPSHOT_SELECTION.md`.

The all-29 assembly preserves the original final `BATCH_JOBS.json` selection and overrides only Ch6_U4 and Ch7_U02 with the corrected original-local replay results. `evidence/ROOT_FULL_BODY_DIFF.json` records the permitted `[P0011]` to `[11]` change.

`MANIFEST.sha256.json` lists every staged file except the manifest itself (54 entries) with its relative path, byte size, and SHA-256. `SAFETY_SCAN.json` covers the 53 non-metadata content files (984,792 bytes) and reports zero credential-like and signed-URL-like matches. The scan records counts and paths only; it never writes matching values. The complete staging directory is 55 files and 995,238 bytes including these two generated metadata files.
