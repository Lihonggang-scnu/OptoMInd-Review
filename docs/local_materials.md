# Local material snapshots

The upgrade-3 local material layer turns a local TEI or JATS XML document into
an immutable snapshot for later reading. It does not choose a research facet,
write claims, or call a model.

Build a snapshot with:

```powershell
python scripts/upgrade3/build_local_materials.py `
  --input paper.tei.xml `
  --output-root outputs/local_materials `
  --canonical-paper-id 10.1234/example
```

Each snapshot directory contains the exact input bytes under `sources/`,
`DOCUMENT_MANIFEST.json`, `DOCUMENT_BLOCKS.jsonl`, `DOCUMENT_ASSETS.json`,
`REFERENCES.json`, `READING_VIEW.md`, and `ACQUISITION_LOG.jsonl`.

`text_raw` is extracted mixed content and `text_normalized` only folds
whitespace. Superscripts and subscripts receive explicit `^{...}` and `_{...}`
markers so scientific values such as `10^-3` cannot collapse into `103`.
Inline citation spans use offsets into the normalized Unicode string and point
to the reference index when a bibliography target resolves. XML paths and the
original source hash remain available for audit. A block locator is the pair
`(snapshot_id, block_id)`; block IDs can repeat in different snapshots and
must not be used alone.

Captions do not prove that an image was read. Figure assets without image bytes
are recorded as `caption_only`, included in `known_gaps`, and surfaced in the
reading view. A filename is never used to mark a paper identity verified;
missing or conflicting DOI/title/year metadata remains provisional or conflict.

Use `PreparedSnapshotProvider` to load a snapshot. It checks source hashes and
referential integrity among blocks, assets, references, and parent links before
returning material. Existing snapshots are immutable: a different input cannot
overwrite a directory with an existing snapshot ID.

## Bounded acquisition

`MaterialAcquirer` accepts a paper record containing a canonical ID, DOI,
Semantic Scholar/OpenAlex IDs, title, authors, year, abstract, and snippets.
It resolves identity through the existing provider adapters, then tries
structured JATS before TEI/HTML and finally an OA PDF through GROBID or the
fitz text fallback. It stops after a usable structured source and does not
call a model. Provider claims, filenames, redirects, login pages, and HTTP
headers are never treated as proof of article content.

The default deadline is 180 seconds per paper. Network workers default to four
and are capped at ten; PDF parsing is serialized at one. Cache blobs and
metadata are hash validated before reuse. `--refresh` creates a new acquisition
revision and leaves the prior snapshot intact; it does not invent a scientific
publication version. Batch progress is written as
ASCII-safe JSON to stdout and to `_batch_checkpoints/`; a checkpoint is reused
only when its record, paired XML/PDF source bytes, implementation/config/GROBID
fingerprint, and normalizer version match and its prepared snapshot passes full
validation. Provider request timeouts are bounded individually; a legacy adapter
that cannot accept a remaining-deadline argument is treated as a soft deadline
until its call returns.

After the structured and provider routes, acquisition may inspect a bounded
set of exact public URLs returned by provider metadata. It preserves OpenAlex
locations and Unpaywall landing pages, records provider errors separately from
an empty search, and requires identity evidence from the fetched source before
counting a body. Search snippets and input titles never become body text.
Candidate source/version fields and the fetched response hash are retained in
the snapshot sources. The default rescue path performs at most two exact-title
or DOI searches and four Firecrawl reader extractions per paper, after ordinary
routes fail. It does not use the general search cache or a Jina fallback, and
each request has the acquisition deadline. Reader requests share a serial lane,
keep one second between calls, and honor a numeric Retry-After on 429 (otherwise
15 seconds). Waiting consumes the paper deadline. The DOI query is only used
when the title search returns no candidates. Lower either reader budget to zero
to disable that operation. `--disable-rescue` disables the extra public rescue;
it does not disable ordinary provider network requests. Use local inputs for an
offline run:

```powershell
python scripts/upgrade3/build_local_materials.py `
  --record-json paper.json `
  --rescue-max-candidates 8 `
  --rescue-max-search-calls 2 `
  --rescue-max-extract-calls 4
```

The CLI injects `FirecrawlReader` automatically. Programmatic callers that
want this online fallback must pass it explicitly; `reader=None` keeps only
ordinary acquisition providers, and the rescue flags do not disable those:

```python
from optomind_research.runtime.upgrade3.material_acquisition import MaterialAcquirer
from optomind_research.runtime.upgrade3.public_reader import FirecrawlReader

acquirer = MaterialAcquirer("outputs/materials", reader=FirecrawlReader())
result = acquirer.acquire(paper_record)
```

HTML/reader projections are `structured_partial` even when the article identity
matches: text extraction does not establish complete tables, formulas, images,
or references. Abstract-only repository pages are rejected as bodies. Reader
responses (including rejected ones) and normalized search candidates are saved
under the acquisition cache for diagnosis. The selected original response is
also retained in the immutable snapshot. A public author or preprint version
may be retained when title, authors, and year support the same work; its DOI
or version conflict remains visible in provenance rather than replacing the
requested paper identity.

Run a frozen manifest with:

```powershell
python scripts/upgrade3/build_local_materials.py `
  --batch-manifest outputs/local_materials_acceptance/20260918/SAMPLE_MANIFEST.json `
  --output-root outputs/local_materials_acceptance/20260918/run `
  --cache-root data/local_material_acquisition_cache `
  --network-workers 4 `
  --deadline-seconds 180
```

The manifest must contain a `records` array. Records may point to `local_xml`
or `local_pdf` paths relative to the manifest, which is useful for offline
fixtures. A normal network record can expose provider IDs and route metadata;
no institution-login resolver is used. For one record, pass `--record-json`.
For a local input, pass `--input` and optionally `--record-json` or `--pdf`.
For an already prepared snapshot, pass `--prepared` to validate and load it.

The final snapshot keeps the selected source bytes plus additional immutable
artifacts: original PDFs, derived TEI, compressed wire bytes, fitz page
positions, CORE inline fullText when supplied, and safe PMC supplementary
bundles/images. PMC graphic bytes are marked readable only after format
validation and remain `provided_to_multimodal: false`. Synthetic XML from an
PDF/abstract/snippet route records an input-attributed identity basis where the
parser supplied no independent identity. HTML/reader routes record source
identity evidence. The
the raw record or source bytes remain hash-bound in `sources/`. Every snapshot
also retains resolved provider metadata, the supplied abstract, and snippets as
an auxiliary source; these are exposed in `READING_VIEW.md` without being
counted as publisher body text.

The view expands distinct auxiliary abstracts and non-title snippets under a
separate evidence heading with source paths and snippet locators. It does not
silently replace a publisher abstract when they disagree. `metadata_only`
means that identity information was retained but no readable scientific text
was obtained; a published snapshot alone is not acquisition success.

For a downstream reader, load `PreparedSnapshotProvider(path).load().as_material()`.
Use `reading_view` for text and the block/asset/reference indexes for targeted
reading and citations. A formula marked available means its source is indexed,
not that PDF extraction preserved its mathematics. PDF-derived formulas,
table structure, scans and figure interpretation may need the original assets.
This layer performs no OCR or image interpretation and makes no Qwen calls.

## Bounded Qwen background reading

`PreparedSnapshot` exposes a small model-input boundary without starting a
reader orchestration:

```python
snapshot = PreparedSnapshotProvider(path).load()
policy = snapshot.reading_policy()
packet = snapshot.build_reading_packet()
messages = snapshot.build_reading_messages(
    "What objective, method summary, and reported findings are explicit?",
    [{"id": "results", "ask": "Which findings are reported?"}],
    packet=packet,
)
normalized = snapshot.normalize_reading_result(model_json, packet=packet)
```

The computed policy inspects actual abstract locators and body sections while
leaving the immutable manifest unchanged. Its `material_scope` is one of
`fulltext`, `structured_partial`, `abstract_only`, `abstract_plus_snippets`,
`snippet_only`, or `metadata_only`. A metadata abstract is retained with
`source_kind=metadata_abstract`; an article abstract uses the neutral
`source_kind=article_abstract`; retrieved snippets remain
`source_kind=retrieved_snippet`. Title hits are excluded from reading text.
Metadata-only packets are ineligible for model input.

Every packet observation carries a stable observation ID, source document and
role, raw-source hash, text hash, locator, paper identity, identity status, and
global gap summary. The system message instructs Qwen to quote explicit source
text, preserve reported numeric findings, and leave unstated details unknown.
`normalize_reading_result` matches each quote against the trusted packet text,
copies provenance from the snapshot rather than the model response, and forces
the trusted packet scope onto every result row. Scope requests from the model
are recorded as overridden; accepted rows are marked
`quote_matched_summary_unverified` with `semantic_truth_status=not_certified`.
The adapter does not certify scientific truth or upgrade abstract/snippet
material to full-text evidence. Supplied packets are rebuilt and compared with
the snapshot's trusted packet before use, so a packet from another snapshot or
with modified text is rejected.

On another host, install the project's Python dependencies and configure the
provider credentials using the existing key configuration. Run GROBID as a
service (for example `docker compose -f deploy/grobid/docker-compose.yml up -d`),
or set `GROBID_URL` to an existing service. The compose service restarts
automatically; no Docker Desktop interaction is required for each paper.
When both services run in containers, use the service hostname in `GROBID_URL`
instead of loopback. Native JATS/TEI inputs work without GROBID.

GROBID is an optional HTTP service at `http://127.0.0.1:8070`; the client does
not require Docker. When available, it requests paragraph, figure, reference,
formula, and bibliography coordinates and records the service-reported version
or `unknown`. If it is unavailable, fitz records page text and positions with
layout/scan gaps instead of claiming recovered structure.

`config/local_materials.yaml` documents the same defaults for deployment and
review. The current CLI reads its explicit command-line options and does not
silently load that YAML file; pass changed values on the command line or wire a
configuration loader before relying on the file operationally.
