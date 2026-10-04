# Public-package security scan

Scan date: 2026-10-04. Scope: this directory only.

- Credentials and key material: absent. Only the sanitized filename `qwen-api-key.txt` is mentioned in configuration metadata; no key text, hash, header, or token is included.
- Authentication headers and bearer values: absent.
- Signed URLs and query tokens: absent.
- SQLite, local index, cache, full B pool, paper fulltext, PDF, and source passage bodies: absent.
- Personal information: none intentionally included.
- User messages containing copyrighted excerpts: absent.
- AI-generated material: explicitly tagged in `real_f3/TOOL_RESULT.md`, `real_f3/COMPACT_SUMMARY.json`, and `real_f3/OUTLINE_RESPONSE.json`.
- Raw model transport envelopes: absent.
- File size rule: every package file is intended to remain below 1 MiB; manifest sizes and hashes use canonical UTF-8 LF bytes so `core.autocrlf=true` checkouts remain stable.

`pack_public.py --check` performs a deterministic local scan for banned names and token/header patterns, verifies the size rule, and validates the manifest hashes. It does not access the network or any credential file.
