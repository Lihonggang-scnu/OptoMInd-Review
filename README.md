# OptoMind: Optical Research Literature Review and Publication Pipeline

[中文说明](README.zh-CN.md)

OptoMind-Review is part of our competition entry developed for the Alibaba Cloud challenge “Research and Application of an AI Scientist Based on Domestic Open-Source Large Models” (problem number: XH-202619). Its primary contribution is the end-to-end delivery of a complete English PDF from a user's literature-review request.

Track 1A of the challenge asks participating teams to build an AI application that can address the 125 frontier scientific questions proposed by *Science* through a complete workflow covering “question understanding → knowledge integration → candidate hypothesis generation → evidence organization → research-plan generation → feedback and revision.” In support of this goal, we have implemented a subset of capabilities for scientific-literature understanding, knowledge integration, and evidence organization as a domain-oriented research harness: OptoMind-Review. It progressively turns a natural-language research question into traceable research materials, organizes retrieved literature, structures section-level evidence, generates cited content, builds visual assets, and produces a literature review together with publication files. The result is a substantially complete automated pipeline from “research question → evidence → review → publication.”

OptoMind-Review has so far been tested and validated mainly in optics and electromagnetics. Domain knowledge and task instructions are configured primarily through the `.txt` prompt files in the `prompts/` directory, so the harness and workflow are not inherently limited to optics. By adapting the prompts for another discipline, the current workflow can be migrated to other scientific domains at relatively low cost.

## What It Does

The mainline workflow includes:

1. Reading the research question and defining the topic, scope, and chapter tasks;
2. Searching for papers, acquiring open full text, and extracting text passages;
3. Building a central material cache and a topic-scoped knowledge base;
4. Selecting evidence by chapter and forming argument materials that connect claims, passages, and papers;
5. Drafting chapters and strengthening their explanatory assets;
6. Handing off and arranging the full manuscript, then generating the title, introduction, abstract, and conclusion;
7. Planning, retrieving, generating, and mounting visual assets;
8. Completing publication metadata and compiling English and Chinese LaTeX/PDF publications.

## Results from Three Complete Tests

We have completed three full end-to-end validations around representative optical topics. Each run began with a simulated user question and proceeded through literature retrieval, evidence organization, chapter authoring, full-manuscript arrangement, and visual-asset processing to produce a readable, verifiable, and low-cost English literature-review PDF. The table below reports both the resulting publication scale and the actual computational investment, so readers can see what the system delivered and what it required.

| Topic | English body | Chapters | References | English PDF | Active runtime | Model calls | Total tokens | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Optical diffractive neural networks | Approx. 13,700 words | 7 | 121 | 37 pages | Approx. 2 h 7 min | 240 | Approx. 8.242 million | ¥5.39 |
| Metasurface holography: from inverse design and fabrication to dynamic display and imaging | Approx. 12,900 words | 7 | 108 | 36 pages | Approx. 1 h 34 min | 444 | Approx. 10.000 million | ¥6.43 |
| Scalable photonic computing: from programmable integrated photonic chips to AI acceleration and optical interconnects | Approx. 16,200 words | 8 | 124 | 44 pages | Approx. 1 h 59 min | 259 | Approx. 9.521 million | ¥9.66 |
| **Total for three tests** | **Approx. 42,800 words** | **22** | **353** | **117 pages** | **Approx. 5 h 40 min** | **943** | **Approx. 27.764 million** | **¥21.47** |

The token total is approximately 24.790 million input tokens and 2.974 million output tokens. “Model calls” counts actual model requests, including chapter review, revision, and necessary retries; it does not count tool calls. Runtime uses active wall time. Word counts are measured from the final English manuscript body, page counts are the total pages of the English PDFs, and reference counts are taken from the final bibliographies.

The model calls use Qwen text, vision, and image capabilities, specifically `qwen3.7-flash`, `qwen3.5-plus`, `qwen3.6-plus`, and `qwen-image-2.0-pro` for conceptual-image generation in the third test. Text calls cover question understanding, evidence organization, argument development, chapter authoring, abstract and conclusion writing, translation, and polishing. Vision calls handle material understanding and review, while image calls generate conceptual figures. Across the three tests, the visual pipeline included 13 visual-review calls, 8 diagram-specification calls, and 6 image-generation calls, resulting in 5 mounted generated figures; these are visual subtasks included in the 943 total model calls above. Chapter-style governance used separate reviewer and reviser roles, for 44 calls in total (22 reviews and 22 revisions), focusing on paragraph openings and terminology-abbreviation consistency.

It is worth noting that newer and more capable Qwen models have become available by the time of this release, including the 2.4-trillion-parameter MoE flagship Qwen 3.8 Max. We expect that connecting a stronger Qwen model can further improve the quality of the final publications. The current system still mainly uses relatively cost-efficient models such as `qwen3.7-flash` and `qwen3.5-plus`, for two reasons. First, we aim to obtain sufficiently strong results at the lowest practical cost. The three test tasks consumed approximately 24.790 million input tokens and 2.974 million output tokens in total, at an actual cost of only ¥21.47; switching all calls to Qwen 3.8 Max at the current call volume could raise the estimated cost to roughly ¥400. Second, we intentionally use the runs to test the system's own capability boundary: the task decomposition, retrieval, evidence organization, cross-checking, and feedback-revision mechanisms inside the harness are expected to reduce its dependence on the strongest foundation model. In other words, if the harness can still produce acceptable results with a less capable model, that demonstrates the gain provided by the harness itself; upgrading the underlying model on that basis may then yield even stronger publications.

All three validations covered the planned chapters and completed English-publication generation and basic publication checks. The corresponding final publications and static replays are available under [`artifacts/e2e/`](artifacts/e2e/) and [`replay/`](replay/).

## Quick Start

Python 3.11 or newer is required. On Windows PowerShell, create an environment and install the dependencies as follows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-research.txt
```

You can also use `scripts/bootstrap_research_env.ps1` for deterministic environment initialization. To generate PDFs, install a TeX environment that includes `xelatex` and `latexmk`. Without TeX, the system can still generate the research text, bibliography, and LaTeX source unless strict PDF mode is explicitly requested.

## Where to Place API Keys

The public release keeps the `api_keys/` directory and its prepared filename templates, without any real credentials. Users only need to fill in the existing files; they do not need to create or rename `.txt` files:

```text
OptoMind-Review/
├─ api_keys/
│  ├─ qwen-api-key.txt
│  ├─ semantic-scholar-api-key.txt
│  ├─ openalex.txt
│  ├─ core_api.txt
│  └─ Unpaywall.txt
└─ ...
```

At runtime, `api_keys/` is discovered relative to the repository root.

- `qwen-api-key.txt` is used for Qwen/DashScope text, vision, and image calls and is the primary credential required for live model execution. Multiple values may be placed on separate lines; the runtime treats them as one shared pool and rotates among them.
- `semantic-scholar-api-key.txt`, `openalex.txt`, `core_api.txt`, and `Unpaywall.txt` are optional credentials. When they are absent, the system uses public interfaces or local fallback paths where available.

## Run a New Review

The standard entry point is `run_review_harness.py`. For example:

```powershell
.\.venv\Scripts\python.exe run_review_harness.py `
  --question "写一篇关于规模化光子计算的文献综述，阐述从可编程集成光子芯片到 AI 加速与光互连" `
  --execution-profile private_study `
  --no-research-plan `
  --auto-confirm-query-plan `
  --output-root outputs/research_harness_e2e
```

To continue an existing run, use `--run-dir` to point to its checkpoint directory. Before running, you can inspect the command help and execute the local test suite:

```powershell
.\.venv\Scripts\python.exe run_review_harness.py --help
.\.venv\Scripts\python.exe -m pytest -q
```

The optional research-plan branch is disabled in the example above. The expensive cross-section global DAG is also off by default, while the central material cache, evidence binding, and chapter-readiness checks remain active. If longer global relation reasoning is desired, enable it explicitly with `--phase3-llm-dag`; this can extend the runtime to many hours.

## Static Replay

The repository contains read-only replays of the three historical E2E runs. They must be opened through a local HTTP server rather than by double-clicking the HTML file:

```powershell
python -m http.server 18876 --directory replay
```

Then open <http://127.0.0.1:18876/index.html>. When accessed directly through `file://`, the browser may block JSON loading and display an empty page or a 404.

The replay is a presentation layer. The final publications from the three runs are under `artifacts/e2e/`. The original run trees, downloaded paper full texts, and runtime caches are not included in this public copy.

## Project Structure

```text
run_review_harness.py    Main entry point
optomind_research/       Retrieval, caching, evidence, authoring, publication, and visual modules
config/ llm/ prompts/    Configuration, model adapters, and prompts
tests/                   Automated tests
optomind_ui/             Local replay and run-status interface
replay/                  Static replays of the three E2E runs
artifacts/e2e/           Final public publication layer for the three E2E runs
docs/                    Mainline descriptions, release-boundary notes, and module documentation
```

## License

This project is distributed under the [Apache License 2.0](LICENSE). Third-party dependencies, third-party code, papers, full-text content, and images remain subject to their respective licenses and copyright conditions.

<sub>Transparency statement: ChatGPT, Qwen, and other AI-assisted tools were used during code implementation and testing, as well as documentation organization.</sub>
