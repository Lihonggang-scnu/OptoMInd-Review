# Local material snapshots

The upgrade-3 local material layer turns a local TEI or JATS XML document into
an immutable snapshot for later reading. It does not choose a research facet or
write claims. The default XML/acquisition path does not call a model; explicitly
enabled regional transcription can call the configured vision model.

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
fitz text fallback. It stops after a usable structured source. Without explicit
regional transcription it does not call a model. Provider claims, filenames, redirects, login pages, and HTTP
headers are never treated as proof of article content.

The default deadline is 180 seconds per paper. Network workers default to four
and are capped at ten; the default PDF parser lane is serialized. Explicit
Docling configuration has a separate shared process bound; the example runtime
below uses two Docling processes and eight regional requests across papers. Cache blobs and
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
The default acquisition path performs no OCR or image interpretation and makes
no Qwen calls. Explicit regional transcription is described below.

## Optional local Docling and regional transcription

`build_local_materials` can run an explicitly configured local Docling process
and transcribe its detected formula/table crops through the ordinary Qwen
transport and existing project/account ledgers. Native JATS/TEI inputs retain
priority and bypass this PDF supplement, including provider calls. Figure
interpretation remains disabled; figures and linked captions are retained.

PDF body text still follows the GROBID/fitz path. Docling adds layout, original
PDF crops, and regional formula/table transcription to that body; it does not
replace the body extractor.

The initial acquisition CLI and the formal progressive planning CLI share
`make_material_acquirer_factory(args, regional_client_constructor=None)` from
`scripts.upgrade3.build_local_materials`. Build it once, then inject the returned
callable as `make_planning_supplement_runner(..., acquirer_factory=factory)`.
Each source unit gets its own output root while sharing the same Docling adapter,
regional recognizer, concurrency bounds, cache, budget factory and unknown halt.
New PDFs finish this parse/transcription path before their snapshots reach the
reading card. Native XML still bypasses visual transcription. Omitting the
optional factory retains the previous planning acquisition behavior.

Planning requires the same explicit flags; it never discovers an R runtime JSON
or inherits an earlier acquisition's settings implicitly. For example:

```powershell
python -B -X utf8 -m scripts.upgrade3.progressive_review_plan `
  --pool PLANNING_POOL.jsonl --plan PLAN.json --output-dir outputs/planning `
  --cache-root data/local_material_acquisition_cache --deadline-seconds 240 `
  --docling-cli-config docling-cli.json --docling-cache-root cache/docling `
  --docling-max-processes 2 --recognition-config config/regional_recognition/plus.json `
  --recognition-concurrency 8 --recognition-cache-root cache/regional `
  --recognition-run --recognition-ceiling-cny 80 `
  --budget-ledger outputs/run/budget.sqlite --budget-limit-cny 80 `
  --account-ledger outputs/account/budget.sqlite --account-budget-limit 300 `
  --key-file existing-keys.txt --key-index 1 --tokenizer existing-tokenizer.json `
  --run
```

These are explicit runtime choices, not changed generic defaults. A supervised
caller can pass `regional_client_constructor` to the planning CLI's `main`;
the native factory keeps its dual-ledger accounting and synchronized proxy while
the injected constructor adds the caller's request/response gates.

Use an existing isolated Docling installation and already cached model weights.
The runner sets Hugging Face/Transformers offline mode. It neither installs nor
downloads models. Generate a full configuration with that installation's SDK;
ordinary base-class JSON dumps lose concrete options in Docling 2.138.0:

```python
import json
from pathlib import Path
from importlib.metadata import version
from docling.datamodel.pipeline_options import PdfPipelineOptions

options = PdfPipelineOptions(artifacts_path=Path("/absolute/cached/docling/models"),
    do_ocr=False, do_formula_enrichment=False, do_code_enrichment=False,
    do_picture_description=False, do_picture_classification=False,
    do_chart_extraction=False, enable_remote_services=False,
    allow_external_plugins=False)
native = {"packages": {name: version(name) for name in
    ("docling", "docling-core", "docling-ibm-models", "docling-parse")},
    "pipeline_options": options.model_dump(mode="json", serialize_as_any=True),
    "page_range": "all"}
Path("native-docling.json").write_text(json.dumps(native, indent=2), encoding="utf-8")
```

`page_range` defaults to `all` and resolves against the original PDF. An explicit
`[start, end]` selection records partial coverage. Full page coverage only means
layout was run on every page; undetected or inline formula coverage remains
unknown. Scanned body text stays a gap because this runner disables OCR.

Create an explicit adapter file `docling-cli.json` with absolute paths:

```json
{
  "parser_version": "2.138.0",
  "command": ["/absolute/docling/python", "-B", "-X", "utf8",
    "/absolute/repository/scripts/upgrade3/parse_docling_pdf.py",
    "--input", "{input}", "--output", "{output}",
    "--config", "/absolute/native-docling.json"],
  "config": {"packages": {}, "pipeline_options": {}, "page_range": "all"},
  "timeout_seconds": 1200
}
```

Replace the `config` value with the complete object from `native-docling.json`.
Preflight requires the installed package versions and resolved full native
options to match. Old truncated configurations and failed run artifacts remain
separate; they are not silently reinterpreted as the corrected configuration.

The tested RTX3080 deployment uses `--docling-max-processes 2` and
`--recognition-concurrency 8`; see [measured results](verification/pdf-parser-parallel-20261013/README.md).
The reusable [Plus config](../config/regional_recognition/plus.json) enables
that regional bound and permits at most two attempts only for complete replies
with invalid JSON/schema. Transport failures and unknown spend are not retried
automatically. The generic constructor defaults remain conservative (1 local
process and 4 region requests); explicit deployment parameters take priority.

Create `regional.json` to bound each paper's recognition stage independently
of the acquisition deadline:

```json
{
  "model": "qwen3.5-plus",
  "max_regions": 30,
  "concurrency": 4,
  "max_attempts_per_region": 1,
  "deadline_seconds": 600,
  "timeout_seconds": 120,
  "max_pixels": 4194304,
  "formula_output_tokens": 2048,
  "table_output_tokens": 8192
}
```

The default is Plus. To compare a cheaper model on the same automatically
selected crops, explicitly set `model` to `qwen3.5-flash` in another config.
Its Beijing price/capability registration is separate; no automatic downgrade
occurs. Shared crop caches keep local extraction identical while recognition
caches separate the requested models.

Regional requests initially default to four concurrent calls. Set `concurrency`
in the JSON or override it with `--recognition-concurrency 1` for a serial
comparison (supported range 1–32). This is an initial setting, not a measured
optimum. Docling initially runs one subprocess at a time; override its local
bound with `--docling-max-processes` (1–8). The shared adapter/recognizer enforce
these bounds across all PDFs in `--batch-manifest`, even with multiple
acquisition network workers. Local manifest entries also use the existing
`--network-workers` outer pool; checkpoint publication remains on the main
thread. Each local extraction slot is released before
regional recognition starts. Tables dispatch first to reduce their long tail, while
final regions/assets retain their original document order. Each request owns
an independent provider and immutable attempt directory. Shared tokenizer,
factory initialization and dual-ledger operations use a short lock; HTTP/SSE
does not hold it. Both reservations precede every physical send.
The same adapter and recognizer can be passed through `MaterialAcquirer`'s
existing `providers["docling"]` entry for E2E acquisition; no separate batch
driver or reduced network worker count is required.

Other explicit lower-cost candidates are `qwen3-vl-flash`,
`qwen-vl-ocr-2025-11-20` and `qwen3.5-ocr`. For either OCR model explicitly set
`"json_mode": false` and use `"table_output_tokens": 4096` for the bounded
trial. OCR receives all unchanged instructions and crops in one user message;
unsupported thinking and structured-output parameters are omitted. Responses
must be complete JSON or one complete outer `json` code fence, then pass the
same strict formula/table validation. Arbitrary surrounding prose is rejected.
The VL Flash price boundaries conservatively use 32000 and 128000 tokens;
the highest tier covers the remaining supported input through 258048 tokens.

Preview creates a source-bound snapshot with explicit pending regions and
makes no provider calls:

```powershell
python -B -X utf8 -m scripts.upgrade3.build_local_materials `
  --input paper.pdf --record-json paper-record.json --disable-rescue `
  --output-root outputs/materials --cache-root data/material-cache `
  --deadline-seconds 1200 --docling-cli-config docling-cli.json `
  --recognition-config regional.json --recognition-cache-root data/regional-cache
```

Enable paid transcription only by adding the existing budget/account/key
arguments and an explicit absolute cumulative project ceiling. For example,
if settled project spend is 0.03 CNY and this execution may add at most 1 CNY,
the cumulative ceiling is 1.03 CNY:

```powershell
  --recognition-run --recognition-ceiling-cny 1.03 `
  --budget-ledger outputs/run/budget.sqlite --budget-limit 80 `
  --account-ledger outputs/account/budget.sqlite --account-budget-limit 300 `
  --account-mapping-dir outputs/run/account_mappings `
  --key-file /absolute/existing/keys.txt --key-index 1 `
  --tokenizer /absolute/existing/local/tokenizer.json
```

Both ledgers must already exist. Their history is preserved. The physical
transport reserves in both ledgers before sending, uses one key and zero
internal retries, requests streamed actual usage, and settles from that usage.
Image reservations use the local text tokenizer plus a conservative bound for
each image. Provider bytes, usage, effective request options, project receipts,
source/config/crop hashes, and every attempt are retained. No provider failure
is replaced with an invented transcription.

Successful region caches bind PDF content, local layout content, crop bytes,
model, pixel/output bounds, prompt and implementation. Moving snapshot output
or raising scheduling/fee bounds reuses successful results. A local parser
revision can replay saved responses only when source, crops, prompts, exact
messages and model settings still match; changed request inputs require new
recognition. Reparse publishes an immutable `REPARSED_*.json` receipt with the
original response/result hashes and current parser hash, preserving the paid
attempt. Missing optional formula labels become empty; missing uncertainty
metadata becomes explicit `uncertainty_not_reported` and uncertain status.
The original PDF, raw local layout, linked caption/footnote crops and earlier
snapshots remain intact. Saved Docling input can replace the CLI options via
`--docling-document-json document.json --docling-run-json run.json`.

Detected tables are transcribed even when local cells exist; those cells can
misread scientific units. Recognized cells replace the reading projection while
the raw local candidate remains retained separately. Empty returned captions
or notes fall back to the linked native text with explicit uncertainty. Tables
with empty columns and fully rectangular rows can promote the first row
verbatim to headers with explicit uncertainty. Decoded LaTeX control characters
also mark uncertainty; their raw bytes are preserved without guessing commands.
Formula raw LaTeX is retained alongside conservative escaping normalization, which
preserves matrix row separators. Standard, economy, concise, practical and
legacy reading inputs carry these source-bound structures and fidelity fields.

Unfinished regions expose pending, uncertain, error or budget-stopped status.
Only complete responses with invalid JSON/schema can use an explicitly bounded
second attempt. Unknown spend freezes that region and stops new scheduling.
Already sent calls finish and save their receipts; unsent work remains pending.
Completed cached regions can still replay freely after a stop. The pool never
queues more than the configured number of active region tasks, and scheduling
concurrency is recorded as policy rather than changing paid request identity.
A known reservation refusal before HTTP is retried through the real ledger on
the next execution, including unchanged policy after outstanding reservations
settle. Earlier refusal receipts remain immutable. Unknown spend is never
automatically retried. The recognition deadline and region limit preserve all
completed cache entries. Batch checkpoints with unfinished regional recognition
are re-evaluated automatically; validated successful regions replay freely.

An explicitly reviewed saved unknown can be isolated on a new execution with
`--recognition-reviewed-failures reviewed.json`. The file contains
`{"failures":[{"cache_key":"<64 hex>","attempt":1,"result_sha256":"<64 hex>"}]}`.
Before any dispatch, the CLI validates those immutable cache receipts and the
original project/account reservations, which must both remain uncertain and
fully held. The failed region stays an error and is never called again; its
original receipts and held spend are unchanged. Other unsent regions still
require normal budget reservations, and any new unknown stops shared dispatch.
The snapshot includes an immutable `REVIEWED_HELD_*.json` receipt with the
allowlist/result hashes and retained-hold evidence. Successful caches replay
freely. This option does not clear a running recognizer's stop event.

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
