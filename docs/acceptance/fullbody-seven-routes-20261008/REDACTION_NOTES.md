# Public-copy redaction notes

- Local originals remain under `F:\OptoMind-Review-2\outputs\fullbody_writer_local_20261008_40cny`; the public copy uses local path labels and SHA-256 values where available.
- Duplicate `preview_*/*/inputs/*/FULL_BODY_INPUT.json` caches are omitted. One complete generated snapshot is retained at `plain_whole/FULL_BODY_INPUT.json`; a recursive key audit found no `full_text`, `rawpaper`, PDF, or copyright fields. The corresponding route manifests, implementation report, request preview, and plan messages are retained.
- No PDF, raw SQLite database, API-key file, authentication header, signed URL, email address, or private credential value is included.
- JSON fields whose names identify secrets/authentication are replaced with `[REDACTED_SECRET_OR_AUTH_FIELD]`; matching bearer strings, API-key patterns, signed URLs, email addresses, and secret local paths are redacted in text.
- Model bodies, model-generated plans, actual stage messages, raw model returns, usage, route manifests, root reviews, budget summaries, and provenance are retained unless a field matched the redaction rules above.
- The advanced source manifest records 98 original files totaling 33,623,077 bytes; `PUBLIC_FILE_INDEX.json` records the public transformed files and their new hashes. Local audit originals are at `F:\OptoMind-Review-2\outputs\fullbody_writer_local_20261008_40cny`.
- Large UTF-8 text artifacts are split into byte-preserving `.part-NNNN` files with adjacent `.parts.json` manifests. Concatenating each manifest's listed parts restores the original bytes and the manifest records the original SHA-256.

