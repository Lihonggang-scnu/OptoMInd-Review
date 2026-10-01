{
  "policy": "Publisher-originating abstracts/full text/passages, downloaded documents, caches, credentials, signed URL values, and binary databases are omitted or field-redacted.",
  "large_local_only": [
    "new_plan/DETAILED_REVIEW_PLAN.json",
    "body_restore_20261001/plan/DETAILED_REVIEW_PLAN.json",
    "new_plan/RUN_STATE.json",
    "body_restore_20261001/plan/RUN_STATE.json"
  ],
  "excluded_classes": [
    "materials/**/sources",
    "materials/**/cache/blobs",
    "DOCUMENT_BLOCKS.jsonl",
    "READING_VIEW.md",
    "*.pdf",
    "*.docx",
    "*.zip",
    "*.mp4",
    "*.bin",
    "api_keys/**",
    "*.sqlite"
  ],
  "notes": [
    "The stale failed BODY report is preserved verbatim as historical evidence; later successful assembly/reparse artifacts are separate files.",
    "Old baseline code SHA is unknown; old manuscript is labeled comparison-only."
  ]
}
