# External Prompt Directory

This directory stores human-editable system prompts used by the OptoMind research pipeline. The goal is to keep problem understanding, search planning, source parsing, scoring, and quality gates configurable without changing code.

Current mainline prompts:

| File | Node | Output |
| --- | --- | --- |
| `Query Planner.txt` | Query Planner | Problem understanding, scope definition, keyword decomposition |
| `Atomic Relevance Planner.txt` | Scholar Facet Planner | Searchable scholarly facets |
| `Web Lens Context Extractor.txt` | Web Lens Context Extractor | High-information web context summary |
| `Supplemental Scholar Facet Synthesizer.txt` | Supplemental Scholar Facet Synthesizer | Additional scholarly facets only |
| `Source Credibility Auditor.txt` | Source Credibility Auditor | Source type, credibility, use strategy |
| `Query Expansion Agent.txt` | Query Expansion Agent | A small number of additional English retrieval queries |
| `Scraped Page Cache Auditor.txt` | Scraped Page Cache Auditor | Whether a scraped page should be cached or treated as full text |
| `Feature-level Paper Scorer.txt` | Feature-level Paper Scorer | Paper-to-facet relevance scores |
| `Fulltext Quality Gate.txt` | Fulltext Quality Gate | Core full text, review core, auxiliary material, metadata-only, exclusion, or reacquisition decision |
| `Paper Text Card Builder.txt` | Paper Text Card Builder | Structured English paper card from normalized full text |
| `Paper Card English Normalizer.txt` | Paper Card English Normalizer | English-only normalized paper card from legacy card assets |
| `Text Slice Role Profiler.txt` | Text Slice Role Profiler | English role labels for structured text slices |
| `Visual Chunk HQ Tagger.txt` | Visual Chunk HQ Tagger | High-quality visual chunk profile from image, caption, and nearby text |
| `Review Example Structure Synthesizer.txt` | Review Example Memory Builder | Structural review-writing patterns from top-review PDFs |
| `Review Blueprint Consensus Refiner.txt` | Review Blueprint Consensus Refiner | Critique and refine the visual-aware blueprint |

Maintenance rules:

- Each prompt must assume that the model has no hidden project memory.
- Each prompt should describe only one concrete task.
- All prompts must be written in English.
- All Qwen-facing prompts must require English output unless the node is explicitly a final translation node.
- JSON-output nodes must specify a strict schema that can be validated by code.
- Do not place API keys, credentials, passwords, or private account details in prompt files.
