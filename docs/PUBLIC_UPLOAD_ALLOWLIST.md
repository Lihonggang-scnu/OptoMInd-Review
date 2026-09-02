# Public upload allowlist

Use this list when the owner approves the GitHub upload. It is intentionally
an allowlist: do not use `git add .` in this dirty working tree.

## Include

Include the following source and delivery areas after the final review:

```text
README.md
pyproject.toml
requirements-research.txt
domain_config.yaml
run_review.py
run_review_harness.py

config/                         # templates and runtime policy, no secrets
llm/
optomind_research/
tools/academic_backends/
prompts/
schemas/
tests/
skills/                       # command guidance and provenance metadata
api_keys/                     # directory and reviewed zero-byte placeholders only

optomind_ui/                    # source plus the current static UI build
optomind_desktop/               # source/config/lockfiles only

scripts/bootstrap_research_env.ps1
scripts/build_release_candidate.py
scripts/export_static_replay.py
scripts/__init__.py
scripts/run_review_lead.py
scripts/build_staged_manuscript_context.py
scripts/publication_deployment_preflight.py
scripts/resolve_publication_metadata.py
scripts/run_dominant_review_materialization_real.py
scripts/run_full_review_smoke.py
scripts/run_phase2_phase3_closed_loop_acceptance.py
scripts/run_section_authoring_smoke.py
scripts/run_dominant_review_trigger_manifest.py
scripts/run_dominant_review_expansion_real.py
scripts/run_research_worker_smoke.py
scripts/run_bilingual_publication.py
scripts/run_chapter_asset_enhancer.py
scripts/run_staged_article_completion.py
scripts/run_visual_evidence_factory.py
scripts/verify_public_release.py

replay/
artifacts/e2e/
artifacts/e2e-full-manifest.json
artifacts/e2e-public-layer-manifest.json

docs/MAINLINE_FILE_MAP.md
docs/OPEN_SOURCE_RELEASE_CHECKLIST.md
docs/E2E_ARTIFACTS_INDEX.md
docs/PUBLIC_UPLOAD_ALLOWLIST.md
docs/WORKSPACE_CLEANUP_20260829.md
docs/ARTICLE_COMPLETION_IMPLEMENTATION.md
docs/ASSET_ROLES_AND_STARTING_POINT.md
docs/BILINGUAL_PUBLICATION_PIPELINE.md
docs/CHAPTER_ASSET_ENHANCER.md
docs/FULL_MANUSCRIPT_HANDOFF.md
docs/FULLTEXT_ACQUISITION_GUIDE.md
docs/HARNESS_OBSERVABILITY.md
docs/HARNESS_TOPIC_SAFETY.md
docs/LATEX_PUBLICATION_PIPELINE.md
docs/LONG_TERM_MATERIAL_CACHE_MAINLINE.md
docs/PUBLICATION_METADATA_RESOLUTION.md
docs/STAGED_ARTICLE_COMPLETION.md
docs/STAGED_PUBLICATION_HANDOFF.md
docs/VISUAL_ARGUMENT_ALIGNMENT.md
docs/VISUAL_EVIDENCE_FACTORY_IMPLEMENTATION.md
docs/visual_asset_protocol_v31.md
docs/visual_chunk_hq_pipeline.md
```

The `optomind_desktop/` entry means its source and manifest files only. Do not
include `node_modules/` or `src-tauri/target/`. The `optomind_ui/static/dist/`
files are included because the desktop shell and static preview use the built
interface; rebuild them only when the source UI changes.

## Exclude

Never upload these paths or classes of files:

```text
api_keys/* with non-empty credential values
.env*
*.key
*.pem
*.p12

outputs/
data/
database/
literature_workspace/
user_fulltexts/
archive/
.venv/
.uv-cache-review/
**/node_modules/
**/target/
```

Also exclude the separate parallel handoff line, internal memory/work-order
folders, historical agent work logs, temporary attachments, old one-off
fixtures such as `scripts/run_legacy_replay_9e86860a.py` and
`scripts/run_two_chapter_offline_fixture.py`, and any local scratch directory.
The three raw E2E trees stay in
`outputs/` for local replay and audit; their complete relative-path/hash index
is the portable record. The final publication layer is the only E2E content
selected for the public upload.

## Final command shape

After the owner reviews the allowlist and explicitly approves, add the selected
paths in small groups and inspect the staged diff before committing. The final
command must be assembled from this list, not from a broad recursive add. The
pre-upload verifier is:

```powershell
python scripts/build_release_candidate.py
python scripts/verify_public_release.py
```

These commands are local checks only. They do not create a commit or contact
GitHub. The verifier does not read credential contents; before staging, the
release operator must clear every file under `api_keys/` and add only the
reviewed zero-byte placeholders explicitly.
