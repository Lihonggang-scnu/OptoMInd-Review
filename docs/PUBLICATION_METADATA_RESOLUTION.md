# Publication Metadata Resolution and Audit

The publication metadata resolver turns every `[REF:identity]` marker used by
the latest staged manuscript into an auditable bibliography catalog.  It is
local-first, deterministic, relocation-safe, and refuses to fabricate
bibliographic facts.

## Purpose

The staged manuscript (`STAGED_COMPLETE_REVIEW_EN.md`) carries compact REF
markers such as:

```text
[REF:doi:10.1007/s11831-025-10448-9]
[REF:s2:2c5e4bccf8f358baca8b7c0ad8fb63279ad791f6]
[REF:identity-fallback:cb326fdcbd36f94d]
[REF:037e66f37189104d61c0d000de0cee202b7eec9e]
```

The resolver inventories every marker, resolves each identity to
title/authors/year/venue/DOI/URL with field-level provenance, deduplicates the
same publication referenced through different identities, and emits a
machine-readable catalog plus audit counts for the LaTeX renderer.  It never
alters manuscript prose or REF markers.

## Resolution precedence

For each identity, fields are filled from the most trusted source that
actually carries the field:

1. **Exact local metadata** – unified handoff section files
   (`UNIFIED_MANUSCRIPT_HANDOFF.json`), authoritative enhanced-section input
   packets (`evidence_packets` / `literature_coverage.sources`), explanatory
   citation ledgers, staged manuscript context
   (`STAGED_GLOBAL_INPUTS.json`), long-term material caches, and the local
   Semantic Scholar response cache (`database/s2_cache/s2_online_cache.sqlite`).
   Authoritative input packets outrank background ledgers; core-evidence
   records outrank background records.
2. **Supplemental metadata** – optional repeatable auditable JSON files
   (e.g. a reviewed local bibliography) that fill missing fields.  They sit
   below input packets/ledgers and above provider enrichment and title
   fallback, and can never silently override clean higher-trust exact local
   fields.  See the supplemental input section below.
3. **DOI enrichment** – when a DOI is known and fields are still missing, the
   existing Crossref backend may be used to fill only missing fields
   (opt-in, off by default).
4. **S2 enrichment** – when an S2 paper id is known, the existing Semantic
   Scholar gateway may be used to fill only missing fields (opt-in, off by
   default).
5. **Title fallback** – a title recovered from a local title-only record (no
   DOI/S2/author/year/venue corroboration) is emitted only with an explicit
   `provenance.title` record carrying `source: "title_fallback"`,
   `base_source`, `confidence: "low"`, and a reason.

Provider calls are injectable (`crossref_provider`, `s2_provider`), so
multi-key routing can be layered on later without changing the resolver.
Offline runs never call providers, and no test makes a live network call.

## Supplemental metadata input

`--supplemental-metadata FILE` is repeatable and accepts either a JSON list of
records or an object with a `records` list.  Each record carries one or more
identities and/or a title, bibliographic fields, and **mandatory provenance**:

```json
{
  "schema_version": "optomind.publication_metadata_supplement.v1",
  "records": [
    {
      "identities": ["hash:abababababababab"],
      "title": "Title-Only Paper Recovered From Review",
      "authors": ["Recovered Author"],
      "year": 2024,
      "venue": "Review Venue",
      "doi": "10.1000/reviewed",
      "url": "https://doi.org/10.1000/reviewed",
      "provenance": {
        "source": "review_bibliography",
        "source_path_or_url": "outputs/review/REVIEW_BIBLIOGRAPHY.json",
        "reason": "recovered from reviewed bibliography"
      }
    }
  ]
}
```

Rules:

- Missing file, malformed JSON, a non-record entry, missing `provenance`, or a
  provenance object without `source` / `source_path_or_url` / `reason`
  refuses with a clear error naming the file and record index.
- A record needs at least one identity or a title; otherwise it cannot be
  linked and is refused.
- Records are linked by exact identity first (prefix-tolerant DOI/S2/hash),
  then by normalized title, but only for entries that currently have a
  title-only record with no DOI/S2 corroboration — never a fuzzy global title
  join.
- Supplemental records flow through the same field-quality guard, aliases,
  DOI/S2/title dedupe, and audit mechanisms as local records.  Corrupt
  supplemental values are rejected and counted like any other source.
- Trust ladder: `input_packet` > `explanatory_ledger` >
  `supplemental_metadata` > `staged_context` > `material_cache` > `s2_cache` >
  `crossref` > `s2_provider` > `title_fallback`.  Supplemental fields get
  explicit provenance (`source: "supplemental_metadata"`, `base_source` from
  the file's provenance, `source_path` from `source_path_or_url`, confidence,
  and the match kind), and clean higher-trust local fields are never
  overridden.
- Supplemental files are included in `input.input_files` and therefore in the
  relocation-safe fingerprint, so reruns remain byte-identical.

## Hard rules

- Never fabricate year, authors, venue, or DOI.
- Never substitute `1900` as a completeness claim.  A source year of `1900` is
  treated as an unknown/placeholder year: the field stays empty and the
  rejection is recorded in `resolution_notes` and the audit
  (`placeholder_year_1900_rejected_count`).
- Placeholder author/venue tokens ("Authors not recovered…", "Metadata
  pending", "unknown") are rejected; the fields stay empty.
- Corrupt publication text (Windows mojibake) is rejected at metadata
  selection, never patched into `??` at rendering.  See the text-quality
  guard below.
- Unresolved fields remain empty with `provenance.<field>.status = "missing"`
  and reasons.
- DOIs recovered from `m3gap:` chunk ids are validated against the DOI pattern
  before use and marked as derived in the record.

## Metadata text quality (mojibake guard)

Local metadata sometimes contains Windows mojibake (e.g. `Far??ield`,
`End??o??nd`, or author names with isolated CJK characters).  Because local
trust outranks providers, corrupt local values would otherwise block clean
provider fields.  The resolver therefore applies a conservative, generic
quality check to title, authors, and venue at selection time:

- A **Latin-dominant** string containing a replacement character (`U+FFFD`) is
  unusable (`replacement_character_in_latin_dominant_text`).
- A **Latin-dominant** string with CJK characters (Han/Hiragana/Katakana/
  Hangul) **embedded inside Latin tokens** — adjacent to a Latin letter with
  no separator, as in `End鍦?nd` — is unusable
  (`cjk_sequences_in_latin_dominant_text`).  "Latin-dominant" means the string
  has at least one Latin letter and at least as many Latin letters as CJK
  characters.  Legitimate bilingual names that keep CJK as their own token
  (e.g. `J. Guo 郭`) are not treated as corruption.
- A corrupt author anywhere in an author list makes the whole authors field
  unusable, so a clean provider author list can replace it.
- **Genuine predominantly-CJK metadata is never blanked**: Chinese titles,
  Chinese author names, and Chinese venues survive the guard.
- Normal accented Latin names (`José Álvarez`, `Frédéric Müller`) survive.

No per-paper titles or replacement mappings are hard-coded.  Rejected fields
are treated exactly like missing fields: they stay empty with the rejection
reason in `provenance.<field>.reasons`, `resolution_notes`, and a new
per-entry `quality_rejections` list (`field`, `reason`, `source`,
`source_path`).  Because they are empty, a DOI/S2 provider candidate fills
them; clean local fields are never overridden.

## Provider enrichment trigger

Providers are only consulted when a stable DOI or S2 identity exists and any
of `title` / `authors` / `year` / `venue` is missing or corrupt.  This lets
Crossref/S2 fill a missing or corrupt venue even when the rest of the record
is complete, without overriding clean local fields.  Provider caps
(`--max-provider-calls`) and offline-by-default behavior are unchanged.

## Identity model

`parse_ref_identity` normalizes every REF token into a typed identity:

| Token form | Kind | Canonical key |
| --- | --- | --- |
| `doi:10.xxxx/...`, `10.xxxx/...` | `doi` | `doi:<lowercase>` |
| `s2:<40-hex>`, bare 40-hex | `s2` | `s2:<lowercase>` |
| `identity-fallback:<hash>` | `identity-fallback` | `identity-fallback:<hash>` |
| bare 8–64 hex (non-40) | `hash` | `hash:<lowercase>` |
| `corpusid:<id>` | `corpusid` | `corpusid:<id>` |
| `arxiv:<id>` / `pmid:<id>` | `arxiv` / `pmid` | prefixed key |
| anything else | `other` | `other:<casefold>` |

Lookup is prefix-tolerant (a bare 40-hex token matches `s2:` records and vice
versa; an `identity-fallback:` token matches bare-hash records).

## Deduplication

Entries collapse by canonical key in this precedence order:

1. canonical DOI (`doi:<lowercase>`)
2. S2 paper id (`s2:<40-hex>` or `corpusid:<id>`)
3. normalized title (NFKC, lowercased, punctuation stripped)
4. the original identity token for truly unresolved entries

Alias identities are never lost: every REF token and every local identity
(DOI, S2 id, marker id, paper id, handle) is retained in `entry.aliases`, and
`entry.markers` keeps the exact marker mapping with per-marker counts.

## Outputs

### `PUBLICATION_METADATA_CATALOG.json`

```json
{
  "schema_version": "optomind.publication_metadata_resolver.v1",
  "entries": [
    {
      "identity": "doi:10.1007/s11831-025-10448-9",
      "canonical_identity": "doi:10.1007/s11831-025-10448-9",
      "aliases": ["doi:10.1007/s11831-025-10448-9", "X01", "10.1007/..."],
      "markers": ["doi:10.1007/s11831-025-10448-9"],
      "marker_count": 1,
      "sections": ["Introduction"],
      "title": "...", "authors": ["..."], "year": "2025",
      "venue": "...", "doi": "...", "url": "...",
      "provenance": {
        "title": {"source": "explanatory_ledger", "confidence": "high",
                  "source_path": "outputs/.../EXPLANATORY_CITATION_LEDGER.json",
                  "reason": "exact identity match in local metadata"},
        "year": {"status": "missing", "reasons": ["..."]}
      },
      "resolution_status": "resolved | partial | unresolved",
      "missing_fields": [],
      "resolution_notes": []
    }
  ],
  "records": {
    "doi:10.1007/s11831-025-10448-9": { "paper_id": "...", "title": "...",
      "authors": [...], "year": "2025", "venue": "...", "doi": "...",
      "url": "...", "reference_kind": "article", "resolution_status": "resolved",
      "missing_fields": [], "markers": [...], "marker_count": 1 },
    "<every alias>": { ... same record with that paper_id ... }
  },
  "audit": { ...totals... },
  "input": { "staged_manuscript": "...", "unified_handoff": "...",
             "input_files": [{"path": "...", "sha256": "..."}] },
  "input_fingerprint": "...",
  "catalog_fingerprint": "..."
}
```

`records` is keyed by the canonical identity **and every alias**, so the
existing LaTeX renderer can consume it directly as
`BIBLIOGRAPHY_METADATA.json`-style records without losing marker mapping.

## Compatibility with the LaTeX renderer

This component is a standalone bibliography repair layer, not a re-implementation
of the renderer.  The renderer's own `resolve_publication_metadata` handles
article-level front matter; this resolver only produces the bibliography
contract the renderer already reads (`records` keyed by `paper_id`, with
`title` / `authors` / `year` / `venue` / `doi` / `url` / `reference_kind` /
`metadata_source`).  The renderer is intentionally not modified; it can load
this catalog's `records` map (optionally copied into its
`BIBLIOGRAPHY_METADATA.json`) and will now receive empty missing fields plus
resolution status instead of fabricated `1900` years or "Metadata pending"
placeholders.

### `PUBLICATION_METADATA_AUDIT.json`

Schema + fingerprints + the full `audit` block:

- `total_ref_markers`, `unique_ref_identities`, `catalog_entry_count`
- `deduplicated_identity_count`
- `resolution_status_counts` (resolved / partial / unresolved)
- `identity_kind_counts`, `missing_field_counts`, `source_counts`
- `enriched_by_crossref_count`, `enriched_by_s2_count`, `provider_calls`,
  `provider_errors`
- `placeholder_year_1900_rejected_count`, `malformed_ref_count`
- `corrupt_metadata_field_rejections` and
  `corrupt_metadata_field_rejections_by_field`
- `supplemental_metadata_file_count` and `supplemental_metadata_record_count`
  (only explicitly loaded `--supplemental-metadata` files/records are counted;
  ledgers, packets, caches, and staged context are source files but never
  supplemental files)
- field coverage totals (`with_doi_count`, `with_title_count`, …)

Resolution status definitions:

- `resolved` – title, authors, and year present, plus a venue or a
  DOI/URL locator.
- `partial` – title or a stable locator present, but required fields missing.
- `unresolved` – no title and no locator; fields stay empty and transparent.

## Determinism and relocation safety

- All ordering is stable (input order for markers, sorted keys elsewhere).
- Source paths are stored project-relative; fingerprints depend only on
  content, never absolute paths.
- `catalog_fingerprint` is the sha256 of the canonical JSON of the catalog;
  rerunning on identical inputs yields byte-identical catalog files.
- Large auxiliary caches are hashed via the catalog content rather than
  re-reading gigabytes of JSON.

## CLI

```powershell
py -3.11 scripts/resolve_publication_metadata.py `
  --staged-manuscript outputs/staged_article_completion_r34_20260815_live_v1/STAGED_COMPLETE_REVIEW_EN.md `
  --handoff outputs/full_manuscript_handoff_r33_20260815/UNIFIED_MANUSCRIPT_HANDOFF.json `
  --project-root . `
  --output-dir outputs/publication_metadata_resolution
```

Offline by default (local-first).  Optional flags:

- `--online` – allow live Crossref and Semantic Scholar enrichment.
- `--crossref-only` / `--s2-only` – restrict live enrichment.
- `--max-provider-calls N` – safety cap on total provider calls.
- `--material-cache-dir DIR` – scan an extra long-term material cache root
  (repeatable; the latest snapshot is used).
- `--supplemental-metadata FILE` – repeatable auditable supplemental metadata
  JSON (see the supplemental input section).
- `--no-material-caches` / `--max-material-cache-roots N` – control
  auto-discovered cache roots (resource guard, not a metadata cap).
- `--staged-context PATH` – explicit `STAGED_GLOBAL_INPUTS.json`
  (auto-discovered under `outputs/staged_context_*` when omitted).
- `--no-s2-cache` / `--s2-cache-path PATH` – control the local S2 response
  cache.
- `--no-digest-verification` – skip sha256 verification of handoff-referenced
  files.

Exit code `0` prints a compact JSON summary; `2` prints `REFUSED: <reason>` for
missing/malformed inputs.

## Tests

```powershell
py -3.11 -m pytest tests/test_publication_metadata_resolver.py -q
```

Focused mocked/local tests cover: explanatory-ledger resolution, mocked
Crossref DOI enrichment, mocked Semantic Scholar S2 enrichment, input-packet +
chunk-id DOI recovery, material-cache and S2-cache local resolution,
deduplication (DOI → S2 → title) with alias retention, unresolved
transparency, the no-1900 rule, explicit title-fallback provenance,
deterministic reruns, clear input errors, and offline-no-provider guarantees.
No test makes a live network call.

## Scope and safety

The resolver only reads manuscript prose and local metadata; it never rewrites
the manuscript or REF markers, never touches staged/commander/visual modules,
and never writes to existing generated outputs.  Only the catalog and audit
files under the requested output directory are created.
