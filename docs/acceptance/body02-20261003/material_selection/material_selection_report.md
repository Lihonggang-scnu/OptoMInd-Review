# Body02 real-material selection (2026-10-03)

Read-only triage of existing snapshots from the acceptance outputs. No network retrieval, credential/key inspection, or source-code edits were performed. The two recommended inputs are fulltext reviews in different domains and have existing A/B paper cards.

## Recommended pair

### 1. Microbiome / ICI review (recommended first live call)

**Identity and material.** `Roles of microbiota in pancreatic cancer development and treatment` (M. S. Cruz, J. Tintelnot, N. Gagliani; Gut Microbes, 2024; DOI `10.1080/19490976.2024.2320280`). The readable snapshot is:

`F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\materials\00d3d83d6571a7d9c15adbb84e0c371ce46d15a4\snapshot-5be0c4b8670f4d78bd64a766`

Use `READING_VIEW.md` (114,004 bytes; declared `fulltext`), `DOCUMENT_BLOCKS.jsonl`, and `sources\main.xml`. The identity/source metadata is `sources\observed-metadata-2df22a03a122.json`; its provider record is verified and carries canonical paper id `00d3d83d6571a7d9c15adbb84e0c371ce46d15a4`.

**Index and card evidence.** In `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\planning_level1\planning_material_index.sqlite`, this row is `source_handle=P0004`, `material_depth=fulltext`, `identity_status=pool_identity`, `pool_action=already_in_pool`; the row's `card_path` points to:

`F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\00d3d83d6571a7d9c15adbb84e0c371ce46d15a4\ff1ff79c0b843ce0db21e51a7032e870\attempt-01\PAPER_READING_CARD.json`

The human-readable A/B card is next to it as `PAPER_READING_CARD.md` (`paper-card-a4073a7e00cb992b63a2b819`). Its B section supplies the expected directed-reader focus: ICI mechanisms, PDAC-specific limits, and the contrast between PDAC evidence and melanoma/CRC analogies. There is no exact P0004 row in the current run's `planning_level1\directed_reading.sqlite`; treat the B-section questions as the expected directed-reader contract rather than claiming an already executed directed read.

**Source-paragraph facts (actual headers and excerpts).** Under `## [block-f744984b95a559b4ac4c] Influence of the microbiota on immunotherapy`, the body says that Bifidobacterium, *A. muciniphila*, and *E. hirae* increased anti-PD-1 efficacy in vivo and links this to dendritic-cell maturation, IL-12 secretion, CD8+ T-cell priming/infiltration, and CCR9+CXCR3+CD4+ recruitment. Under `## [block-be6e1d6ee0c7a33c4f14]` in the same section, it states that bacterial ablation rendered an otherwise anti-PD-1-insensitive orthotopic PDAC model sensitive, with increased CD4+/CD8+ T-cell activation. Under `## [block-6c586eb4edf3f498947c] Microbiota plays a role in PDAC therapy efficacy and resistance`, the review separately discusses chemotherapy, toxicity, and metabolites, so ICI claims should not be merged with those chemotherapy claims.

**Two source-answerable questions for the live test.**

1. Which microbiota-to-ICI routes does this review explicitly document (microbe or depletion → DC/IL-12, CD8+, CD4+/Treg, or MDSC change → anti-PD-1/CTLA-4 outcome), and what model or cohort condition accompanies each route?
2. What does the review actually establish for PDAC about depletion and anti-PD-1 sensitivity, and which positive microbiome–ICI examples are explicitly from melanoma/CRC or other tumour models rather than PDAC patients?

### 2. Solid-state-electrolyte review (different domain)

**Identity and material.** `Sulfide and Oxide Inorganic Solid Electrolytes for All-Solid-State Li Batteries: A Review` (Nanomaterials, 2020; DOI `10.3390/nano10081606`). The readable snapshot is:

`F:\OptoMind-Review-2\outputs\planning_revision\holdout_x2_preparation\materials\03c787f67836e02cfb9fdf0825b73a1f5d90b825\snapshot-12aa84283ca0682f0409f5f7`

Use `READING_VIEW.md` (375,992 bytes; declared `fulltext`), `DOCUMENT_BLOCKS.jsonl`, and `sources\main.xml`. The metadata file is `sources\observed-metadata-762ea99f228d.json`.

**Index and card evidence.** In `F:\OptoMind-Review-2\outputs\planning_revision\holdout_x2_fullchain\planning_material_index.sqlite`, this row is `source_handle=P0001`, `material_depth=fulltext`, `identity_status=pool_identity`, `pool_action=already_in_pool`; its A/B card is:

`F:\OptoMind-Review-2\outputs\planning_revision\holdout_x2_preparation\ab_run\cards\03c787f67836e02cfb9fdf0825b73a1f5d90b825\7c224aa99df1eb811a395c503173d4c8\attempt-01\PAPER_READING_CARD.md`

(`paper-card-8af5ac20054e0057766b6142`). The card's B section proposes the sulfide-versus-oxide comparison, ionic-conduction mechanisms, interface stability, stack pressure, sintering, and fabrication conditions. The Reading View is 375,992 bytes; bytes are not tokens, so this size does not by itself exceed a 250k-token source-input limit. No manual excerpting is required on the byte count; apply the actual production preflight if it imposes a separate token or context limit.

**Source-paragraph facts (actual headers and excerpts).** `## [block-64667178ef2427e99e0b] 2. Ionic Conduction in the Solid State` and `### 2.1. Ionic Conduction` explain vacancy/defect hopping and give the conductivity relation. `## [block-02020ccd98846b6b734e] 3. Sulfide Solid Electrolytes` states that sulfides have high room-temperature Li+ conductivity and are softer/deformable, then names reported systems and fabrication/stack-pressure emphasis. `## [block-6c586eb4edf3f498947c] 4. Oxide Solid Electrolytes` states that oxide electrolytes are more air-stable/easier to handle and points to a table of room-temperature conductivity and activation-energy values. The body then has explicit route headings `3.1 Argyrodite`, `3.2 Lithium Phosphorus Sulfide`, `3.3 Li7P3S11`, `3.5 LGPS-type`, `4.1 Garnet`, `4.2 Li-analogues of NASICON`, and `4.3 Perovskite`; it also cites original study examples such as Deiseroth et al. (argyrodite) and Goodenough et al. (NASICON precursor).

**Two source-answerable questions for the live test.**

1. How does this review compare the ion-transport mechanism and measured conductivity/activation-energy trade-offs of sulfide routes (Argyrodite, Li3PS4/Li7P3S11, LGPS) versus oxide routes (garnet, NASICON, perovskite), with the named material examples and conditions retained?
2. Which fabrication and interface variables does the review tie to performance (cold pressing/stack pressure for sulfides, sintering/contact for oxides, and electrode/electrolyte reactions), and which statements are literature summaries rather than new experiments by the review authors?

### Backup candidate (if a newer solid-electrolyte review is desired)

`Recent advances in inorganic solid electrolytes for lithium-ion batteries`, source handle `P0008`, current pool `already_in_pool`, fulltext snapshot:

`F:\OptoMind-Review-2\outputs\planning_revision\holdout_x2_preparation\materials\454191a65e1be9ba7af5c8ef7d3e772affef89b2\snapshot-f95705bd31fdcff6b6a8f7ca`

Its `READING_VIEW.md` is 258,375 bytes; this is a byte count, not a 250k-token count. It has section headers for ionic migration, synthesis methods (solid-state reaction, sol-gel, melt-quenching, microwave, solution, ultrafast), conductivity parameters, and garnet/NASICON/perovskite/sulfide families. It is a good alternate; use production preflight to decide whether any excerpting is needed.

## Corresponding current-run owner packet

Use the existing current-run mechanism owner packet:

`F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\planning_level1\writer_packets\CH02.md`

Owner/title: `CH02 — 微生物组调节 ICI 疗效的分子与细胞介导机制`. The small relevant subset is `CH02:U1` (microbial metabolites → receptors/epigenetic programs → CD8+/myeloid effects) plus the first paragraph brief of `CH02:U3` (PAMPs/OMVs and antigen cross-reactivity). This subset is appropriate for P0004's two questions; it should not be used to invent PDAC patient-level causal claims. The packet itself lists evidence-boundary cautions and distinguishes human association, animal causality, and in-vitro evidence.

