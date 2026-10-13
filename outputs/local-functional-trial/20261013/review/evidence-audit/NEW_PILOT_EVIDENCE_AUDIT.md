# NEW Review pilot evidence audit

## Result

The NEW pilot has **incomplete semantic coverage**. The final schema is structurally well formed: three facets cover the five declared requirements, and two full text papers are within the three paper ceiling. The evidence does not support a full answer to the question. It supports a substantive qualitative physical-bound discussion (F1/R1), contextual design-mechanism discussion (F2), and only secondary/contextual performance discussion (F3). It cannot establish a broad experimental bandwidth--field-of-view--efficiency tradeoff.

`PLAN_RECEIPT.json` records `semantic_coverage: not_assessed`. `CANDIDATE_CORPUS.json` records `degraded: true`, `constraints_status: unapplied_requirements`, and 36 unapplied constraint records (the additional constraints, must-include topics, and synonyms were not executed as retrieval policy). All three facets have `retrieval_hit: true`, but `read_status` and `evidence_sufficiency` remain `not_assessed`. Therefore retrieval hits are not coverage evidence.

## Question and facet coverage

| Facet | Actual support in the selected material | Audit status |
|---|---|---|
| F1 physical bounds on joint bandwidth/FOV | Paper `84c749...` directly states the delay--bandwidth basis, the decrease with NA and FOV, and FOV saturation. The exact bound equations are present as six formula regions but all six are marked `recognition_status: missing`; the flattened parser text is explicitly not faithful. Paper `dd2dc...` adds a conditional single-interface FOV example. | **Partial, strongest facet**: qualitative trend is traceable; exact formula/crossover is not. |
| F2 dispersion-compensation mechanisms and efficiency costs | `84c749...` gives the ideal group-delay/delay-line model and its local/1-D assumptions. `dd2dc...` discusses waveguide versus resonant control, spacing/coupling, multiplexing/crosstalk, and computed oblique-incidence losses from antisymmetric modes. | **Partial**: mechanisms and failure modes are traceable; neither source supplies a primary, broad efficiency tradeoff dataset for visible achromatic devices. |
| F3 demonstrated bandwidth/FOV/efficiency | `dd2dc...` is a perspective that cites an approximately 86% high-NA efficiency and MTF/sidelobe concerns; it reports no new measurements. `84c749...` is theory and has no device efficiency experiment. | **Insufficient direct evidence**: no selected primary experimental paper jointly measures the requested quantities. |

R4 is satisfied operationally (2 selected full-text snapshots, ceiling 3). R5 is a declared scope boundary, not evidence of comprehensive coverage. R3 can be used as an organizing structure, but the demonstrated-performance category is under-supported.

## Provenance and exact source hashes

The pilot inputs were read without invoking retrieval, models, APIs, or planning. SHA-256 values are recorded here so the audit can be reproduced against the same bytes.

| Artifact | SHA-256 |
|---|---|
| `FINAL_SCHEMA.json` | `206a38dc42a5e30ddc8ef064b8a421a69e5a7b22a4b6e0cc1e27ed050f8ec076` |
| `CANDIDATE_CORPUS.json` | `63150f5f0780524cd6f5bce56fe6c7727b5f7f1291e799e993f8d109f113a920` |
| `planning-pool/PLANNING_POOL.jsonl` | `9206c7252d9ac7d3d3d689b36dea118d26b828bc9e339fc1605d5a848877530a` |
| `POOL_RESULT.json` | `08c210be110033d2bdb73f7d065b7083a8b0957baa8a5336da1878fae6901c65` |
| `LOCAL_PDF_SOURCE_MANIFEST.json` | `849cae89dd4225b6545b401cbe8da03b8903d99a865b48a155244cfc75d9a5f1` |

Selected source `84c74988ea8a29fd3a5a26d2655fc7505820f72e` is *Bandwidth bounds for wide-field-of-view dispersion-engineered achromatic metalenses* (DOI `10.1051/epjam/2022012`), theory paper. Card SHA-256: `373c4fe4c632db807ca5ff7bbca48148ba3663492fe513c06a1df1390ae81fa1`. Exact snapshot `snapshot-8645357434f7f7dd690428bf` SHA-256: `451e753bf7fa0fd59eb918d6a5e452f2090b5756d0f9808c3d7fd6c9658b26fb`. Its `DOCUMENT_MANIFEST.json` SHA-256 is `99d430a4031353825f4b213798adc083c1519a87e99c7b19a3bbe7f3a14a32be`; `DOCUMENT_BLOCKS.jsonl` SHA-256 is `53fec2660a09c0a5a4f5c066030ca0f9c6f955b7beb164ab5d18191ca4c60372`; main XML SHA-256 is `941d639c8be73f41569f75f5a53012277b9ca69a483a0bfaa61636c1311bb89e`; PDF SHA-256 is `60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915`.

This paper is labeled **fresh retrieval** for this pilot because it has a `CANDIDATE_CORPUS` paper record and 12 fresh snippet-search hits (F1 and F3). Its later local PDF handoff is a separate material-acquisition layer.

Selected source `dd2dc3ec9c62d3fea69db49d6c9a4c78eb1a6f49` is *Metalenses at visible wavelengths: past, present, perspectives* (DOI `10.1002/lpor.201600295`), a perspective/background paper. Card SHA-256: `c1c8133e0c9f0390cec39f94898c2815f6724edc663f8939938508073053d7dc`. Exact snapshot `snapshot-08c038bb8496aee2485270fa` SHA-256: `e65245ef34b6f490219f7a21f31702d9ac5e2bb09e7777cfa2040484859385fe`. Its `DOCUMENT_MANIFEST.json` SHA-256 is `a53da6d8905fb2baf747b95dedd1109310ea2281dd664cf5f98b17d258f76857`; `DOCUMENT_BLOCKS.jsonl` SHA-256 is `5d1fe1f3cdbbacddddcfb15cd9d679855deb7b91104e367419c0a8b9aa71b82f`; main XML SHA-256 is `e3176f08f91349e2f41980c1d5f88db51e1a6391e5f1023b56291639387eae19`; PDF SHA-256 is `e3e00b4db932b9f95225150b11685dea6276fbc27f9f24408bd54679f5456ec1`.

This paper is labeled **background from the user/Docling handoff local collection, not fresh search**: it has no record and no hit in this pilot's `CANDIDATE_CORPUS`; `LOCAL_PDF_SOURCE_MANIFEST.json` labels it `selected_local_pdf` and points to the old Review-2 acquisition manifest. Both snapshots report `identity_status: input_attributed`, so the audit does not upgrade that to independent identity verification.

## Traceability samples

The following checks use the exact selected snapshot blocks, not the card prose alone.

| Card claim or intended use | Exact source locator | Assessment |
|---|---|---|
| 84c749: bandwidth decreases with NA and FOV, then saturates | `block-954eb9a994ebf4efda2`, page 5, `/texts/59`; also abstract `block-42952c69a6fdf0453edd`, page 1, `/texts/11` | Direct textual support, with the paper's stated large-NA and model scope. |
| 84c749: delay--bandwidth physics bounds local meta-atom group delay | `block-c49e3bd88e30251d93d9`, page 2, `/texts/21`; local 1-D limitation `block-19377701cf3bbbb38e04`, page 6, `/texts/65` | Direct support, but only for the stated local/1-D delay-line model. |
| 84c749: exact upper-bound equations and `r0` cases | Formula blocks `block-a75621d5d94e1ad4e4c5`, `block-37dd84ffc4036130f6cc`, `block-94dd62adb428f65a5048`, `block-ad1fbf52a76f32f0deb0`, `block-0b6b572a837b78635e9d`, `block-800c0484ad89ddf44535`, pages 2/4 | Formula regions are present, but all are explicitly unrecognized; exact algebra and threshold cannot be certified from the flattened text. |
| dd2dc: conditional FOV example and unfair single-lens/commercial-objective comparison | `block-fef079dc219172a584af`, page 12, `/texts/92` | Direct conditional support: NA 0.5, focal length 1 mm, one-wavelength path-delay criterion, visible range. It is not a universal FOV lower bound. |
| dd2dc: wavelength-dependent diffractive efficiency law | `block-fe7a1373ebfb5d59b9b7`, page 12, `/texts/95` | Direct context for diffractive elements; it does not establish a general measured efficiency law for all visible achromatic metalenses. |
| dd2dc: oblique-incidence antisymmetric modes reduce efficiency | `block-8835cbe6a6046ea86e43`, page 9, `/texts/45`; caveat `block-0e6b0590d4ea37afd4c0`, `/texts/46` | Traceable to a computed Huygens-surface model and the authors' interpretation; not a broad primary experimental result. |
| dd2dc: approximately 86% efficiency and MTF/sidelobe concern | `block-d32f51e162c434ab1772`, page 10, `/texts/57` | Secondary discussion of cited Kho16 data; it explicitly calls the 86% value an average and does not supply a joint bandwidth/FOV/efficiency experiment. |

## Retrieval and planning limitations

The 12 planned queries produced 4 full successes, 5 partial results, and 3 failed records. Eight query records contain an HTTP 429 response (eight failed response entries). The three records with `returned: 0` are all 429 failures (`F1_KQ_01`, `F2_QQ_01`, `F3_KQ_02`), not valid zero-result responses. Valid 200/zero-result queries: **0**. The corpus totals 182 raw hits (171 snippet, 11 paper-search) from 70 distinct papers; unresolved paper identities are 0. These numbers describe retrieval behavior, not semantic coverage.

The corrected `planning-02/RUN_STATE.json` is also `stopped_after_level2`, with `chapter_proposals`, `harmonized_scope`, `level2_tools`, and `finalize_chapter_scope` completed and no final BODY artifact observed. Its `level1_tools` result has no provider-failure record: one directed evidence fulfillment is `partial` (P0002/84c749) and one is `unmet` because the shared deep-read budget was exhausted (P0001/dd2dc). These are evidence-fulfillment states, not a retrieval or provider defect. The original planning run remains a separate preserved run; neither planning output upgrades the source evidence.

The corrected chapter roles are: **CH1, “物理界限：色散、视场与带宽的三角制约,”** establishes the local/one-dimensional theoretical bound and uses P0001 only for the single-interface geometry caveat; **CH2, “设计机制：色散补偿策略及其内在效率代价,”** compares propagation-delay and resonant mechanisms, with the oblique-incidence mode-coupling and group-delay-gradient arguments kept at the stated qualitative/model-specific level; **CH3, “实证表现：效率权衡与性能基准的真实图景,”** evaluates efficiency versus MTF/side-lobes and uses P0002 as a qualitative benchmark. The roles organize the review; they do not create a primary experimental dataset or turn the two selected sources into full coverage.

There is a separate final-packet integrity limitation: the stage files contain one proposal each for CH1--CH3, but `PROGRESSIVE_REVIEW_PLAN.partial.json` has no top-level `chapters` or `chapter_proposals` array, and its `shared_outline` contains only `central_question`, `finalized_scope_statement`, and `review_title`. The packet is therefore structurally incomplete even though the intermediate chapter files exist. Its `shared_scope.evidence_base` also labels dd2dc with the provisional title “Perspective on Achromatic Metalenses (Provisional)” rather than the actual selected title recorded above. The finalized scope's directions to reconstruct missing details are compatible with a qualitative plan, but phrases such as broad “efficiency collapse” and locating current experimental points below a theory surface must be retained as bounded interpretations, not accepted as demonstrated results from these two sources.

### Corrected planning and formula provenance

The base/pre-formula A/B source layer remains authoritative for recognition status. In the 84c749 snapshot, the six formula units at `/texts/25`, `/texts/32`, `/texts/34`, `/texts/36`, `/texts/49`, and `/texts/53` (blocks `block-a75621d5d94e1ad4e4c5`, `block-37dd84ffc4036130f6cc`, `block-94dd62adb428f65a5048`, `block-ad1fbf52a76f32f0deb0`, `block-0b6b572a837b78635e9d`, and `block-800c0484ad89ddf44535`) remain `recognition_status: missing`. The dd2dc snapshot's formula unit `/texts/98` also remains missing. The ordinary flattened parser text is therefore not sufficient to certify exact algebra or a crossover threshold.

A local formula cache does exist for 84c749 and is source-bound to PDF SHA-256 `60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915`; its batch result is `success`, and it processed the same six source references. This is an enhanced transcription candidate, not scientific truth: `LOCAL_FORMULAS.json` explicitly records `fidelity_status: local_transcription_candidate_not_scientific_verification`, and the candidates for the third phase expression and group-delay expression contain extra non-formula text. The cache therefore provides a traceable candidate reading for follow-up checking but does not change the base recognition status, certify the equations, or supply missing experimental evidence.

The two selected sources cannot establish:

1. A broad experimental bandwidth--FOV--efficiency tradeoff across visible achromatic metalens architectures. One source is theory with no efficiency experiment; the other is a perspective that cites isolated prior results and computes a specific model.
2. A universal bound or current state-of-the-art claim outside the stated assumptions. The 84c749 bound is local/1-D and idealized; the dd2dc FOV and efficiency discussions are single-interface, diffractive-context, or model-specific and do not cover multilayer, nonlocal, or other architectures.

No fresh-request defect was reproduced or inferred by this read-only semantic audit. The result is evidence insufficiency, not a diagnosis of a fresh-request implementation defect.

## Changed

Created only:

- `D:\OptoMind-Article-3\local-functional-trial\20261013\review\evidence-audit\NEW_PILOT_EVIDENCE_AUDIT.md`
- `D:\OptoMind-Article-3\local-functional-trial\20261013\review\evidence-audit\NEW_PILOT_EVIDENCE_AUDIT.json`

## Verify

Re-read the selected card JSON, `CANDIDATE_CORPUS.json`, `PLANNING_POOL.jsonl`, `POOL_RESULT.json`, `FINAL_SCHEMA.json`, exact `materials-02` manifests/blocks, source PDFs/XML, corrected `planning-02` stage outputs, and the source-bound formula-cache receipt by recorded hash. No production file, source snapshot, database, model, test, API, or planning state was modified.

## Blockers

Semantic coverage remains incomplete: six 84c749 formula units and one dd2dc formula unit are unrecognized in the base snapshots; the 84c749 local formula-cache is only a non-scientific-verification transcription candidate; the corpus left semantic constraints unapplied; corrected planning has one partial and one unmet evidence fulfillment with no provider failure, and its final packet is structurally incomplete; the finalized wording contains overclaim risk if “efficiency collapse” or theory-surface placement is stated as measured fact; and no selected primary experimental paper establishes the requested joint efficiency tradeoff.
