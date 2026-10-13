# Docling runtime acceptance checkpoint

**Audit date:** 2026-10-13  
**Target:** `D:\OptoMind-Article-3\repos\OptoMInd-Review` at `f1544d922538a4a1cec4fe72842472d152dae780` (observed clean)  
**Scope:** read-only evidence review; no new parser/planner execution, API call, GPU work, source-F write, target-repo write, or full regression run.

## Observed route

`review problem -> Docling basic batch -> fulltext snapshots -> 2 cards -> selected paper 84c74988ea8a29fd3a5a26d2655fc7505820f72e -> local formula sidecar -> corrected planning`

The basic receipt at `D:\OptoMind-Article-3\local-functional-trial\20261013\review\docling-basic\BATCH_RESULT.json` is `success`: 2 planned, 2 attempted, 2 unique PDFs, 2 reconverted, 0 cached, worker return code 0. Its receipt-level GPU peak is **3995 MiB** and its parser configuration uses CUDA with remote services disabled. The two document hashes are `369b0c226b6db8f32458f43e9dda5a97db6b93915c75fd553c37bd3a686e7d52` (17 pages) and `caaf0d6f9a5754607fe40f76885678eb2aba77b06f823b923ca380a1abe1b3e3` (7 pages).

The preserved first attempt remains under `review\materials\_batch_checkpoints`: both papers are `metadata_only`, `parser=fallback`, and `selected_route=abstract_snippet_fallback`. The direct sidecar reason is `DoclingAdapterError:docling_crop_renderer_unavailable` with fulltext parser unavailable. The later `materials-02` run is separate and publishes both papers as `fulltext`, `parser=docling`, `selected_route=docling_basic`, body-readable, with complete page coverage. The PyMuPDF install receipt records `pymupdf==1.27.2.3` in `D:\OptoMind-Article-3\local-preparation\envs\review`; the materials-02 Docling receipts still record `PyMuPDF: null`, so the old F environment is not treated as modified.

The card receipt reports `success=2`, finalized manifest, and valid committed cards for both snapshots. The selected paper is `84c74988ea8a29fd3a5a26d2655fc7505820f72e`.

## Formula output and source binding

The formula batch receipt at `D:\OptoMind-Article-3\local-functional-trial\20261013\review\formula-cache\90b70b62eaaf22c2c01cb7afccf5b503b87638d4ee6ba55601ac9449a3fd18c5\selection-054640f2c981107c\results\BATCH_RESULT.json` is `success` for one 7-page PDF. It processed **6/6** requested references (`#/texts/25`, `#/texts/32`, `#/texts/34`, `#/texts/36`, `#/texts/49`, `#/texts/53`), with zero empty and zero unprocessed references. Its receipt-level GPU peak is **3604 MiB**. The run is `CodeFormulaV2`, batch size 32, with `docling 2.138.0`, `torch 2.5.1+cu121`, the F-side formula model cache, and empty API overrides. `cached_count=0` means this formula conversion itself was fresh.

The sidecar `D:\OptoMind-Article-3\local-functional-trial\20261013\review\formula-cache\90b70b62eaaf22c2c01cb7afccf5b503b87638d4ee6ba55601ac9449a3fd18c5\LOCAL_FORMULAS.json` has six `recognized` regions and binds paper `84c74988ea8a29fd3a5a26d2655fc7505820f72e` to PDF SHA-256 `60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915`, raw Docling document SHA-256 `caaf0d6f9a5754607fe40f76885678eb2aba77b06f823b923ca380a1abe1b3e3`, and enhanced document SHA-256 `bfe171991861ee62fbf2ad721d45bbd39dd70ddc97ad65abd282aab75c5d41a7`. Read-only hashing confirmed the selected PDF, raw document, enhanced document, and `run.json` execution receipt (`fa3032535adb4104ab2092bda9b5249cf2ba5078c780b13e1bb65f966bbe4338`) match those bindings.

The sidecar labels the result `local_transcription_candidate_not_scientific_verification`; the formulas remain candidates requiring visual/source review.

## Planner cache evidence

The corrected planner artifact at `review\planning-02` is currently `stopped_after_level2` with report status `partial` and `chapter_count=0`. It consumed the selected paper and wrote a `DIRECTED_MATERIAL_CACHE.json` entry whose `local_formula_config_sha256` equals `CURRENT.json`'s profile hash `50e05980f52080d6e91dae19ed7eabf85490fdcb27655fdf3051df32bb357291`. Both level-1 and level-2 directed-reading prompts contain `structured_source_material` with six `local_formula_status=recognized` formula candidates. No new formula batch result exists inside the planning output.

This proves sidecar/profile consumption by the planner. It does not provide a separate formula-executor cache-hit counter: the only formula batch receipt is the fresh run with `cached_count=0`. The planning artifacts also contain their own qwen directed-reading completions and cost accounting; the API-zero/no-GPU statement above is limited to this audit and the local Docling/formula integration path.

## Review limits

- The preserved first fallback and the later fulltext Docling run are different attempts; they must not be collapsed into one success claim.
- Formula transcription is not scientific verification, and the sidecar's top-level `raw_document_sha256` is null even though its binding field and run receipt carry the verified raw hash.
- Receipt-level GPU peaks are available, but no independent time-series telemetry was reviewed.
- The planner is partial/stopped after level 2; no complete chapter plan is accepted here.
- No full regression suite was run.

**Evidence root:** `D:\OptoMind-Article-3\local-functional-trial\20261013`
