# LaTeX Publication Pipeline

The LaTeX publication stage is a deterministic formatter downstream of the
Review Harness. It does not rewrite scientific content or invent references.

## Inputs

- `REVIEW_CONTENT_PACKAGE.json`
- the final Markdown review
- `FULL_REVIEW_CITATION_MAP.json`
- section source ledgers
- the article-level visual editorial plan
- optional author/publication metadata JSON

## Outputs

- `main.tex`
- `references.bib`
- `main.bbl`
- `main.pdf`
- `arxiv-source.zip`
- `ARXIV_MANIFEST.json`
- `LATEX_BUILD_REPORT.json`
- rendered first/middle/last-page previews

The source archive contains the main TeX file, bibliography files, and only
verified local figures. Build logs, auxiliary files, backups, and the compiled
PDF are excluded from the arXiv upload archive.

## Metadata schema

```json
{
  "title": "Paper title",
  "authors": [
    {
      "name": "Author Name",
      "affiliation": "Institution",
      "email": "author@example.org",
      "orcid": "0000-0000-0000-0000"
    }
  ],
  "abstract": "Optional. If omitted, it is derived from the review blueprint.",
  "keywords": ["keyword one", "keyword two"],
  "date": "",
  "draft_only": false,
  "acknowledgements": ""
}
```

Missing author metadata does not prevent compilation, but the package is marked
`compiled_awaiting_metadata` and is not called submission-ready.

## Command

```powershell
py -3.11 scripts/run_latex_publication.py `
  --content-package outputs/research_harness_e2e/<run>/REVIEW_CONTENT_PACKAGE.json `
  --output-dir outputs/research_harness_e2e/<run>/publication/latex `
  --metadata publication_metadata.json
```

The renderer uses Pandoc for Markdown conversion, `latexmk`/PDFLaTeX for
compilation, BibTeX for references, PyMuPDF for semantic PDF checks, and
Poppler for visual previews.

The generated project follows the current arXiv guidance: compilation starts
from the archive root, figures use PDF/PNG/JPEG, required bibliography files
are included, unnecessary intermediate files are omitted, and the actual PDF
must be visually inspected before submission.

## Deployment preflight

Before running the publication command on a new host, use the dependency-light
preflight:

```powershell
python scripts/publication_deployment_preflight.py `
  --content-package outputs/staged_publication_handoff_r35_20260815_final_v2/REVIEW_CONTENT_PACKAGE.json
```

The preflight uses only the Python standard library. It requires Python 3.11
or newer, checks the publication-only runtime imports by actually importing
them, and reports machine-readable JSON with `status`, `ready`, `blockers`,
`warnings`, `python`, `tools`, `package`, and `checked_at`.

- `--no-compile` permits missing Pandoc, `latexmk`, and `xelatex` as warnings.
- `--no-preview` permits missing `pdfinfo` and `pdftoppm` as warnings.
- `--output-report PATH` writes the same JSON to a file.

Runtime prerequisites and malformed/unsafe package paths produce a nonzero exit
code. `draft_only` or `publication_eligible=false` are policy warnings under
`package.policy_warnings`; they do not make the deployment runtime unready.

### Fresh-shell prerequisites

The saved `DEPLOYMENT_PREFLIGHT.json` can report `ready=true`, but a fresh
deployment shell must still verify its own environment. The preflight remains
strict: do not use `--no-compile` to claim deployment readiness when a compile
tool is absent.

A new host must install:

- Pandoc
- a TeX distribution providing `latexmk` and `xelatex`
- Poppler tools providing `pdfinfo` and `pdftoppm`

Then ensure those commands are on `PATH` and rerun:

```powershell
python scripts/publication_deployment_preflight.py `
  --content-package outputs/staged_publication_handoff_r35_20260815_final_v2/REVIEW_CONTENT_PACKAGE.json
```

Debian/Ubuntu example:

```bash
sudo apt-get update
sudo apt-get install -y pandoc texlive-xetex latexmk poppler-utils
```

On Windows, use the official Pandoc installer, a TeX distribution such as
MiKTeX or TeX Live with XeLaTeX and `latexmk`, and a Poppler-for-Windows
package; add each command's directory to `PATH`, then rerun the same
preflight command. The saved final PDF remains valid; missing Pandoc in a
fresh shell is a host dependency, not a publication-code or PDF regression.
