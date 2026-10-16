"""Preserve the frozen replay site and add publications from the development branch.

GitHub Pages is deployed by lihonggang-dev/.github/workflows/pages.yml.
The legacy checkout is read-only; main and its historical reports are not edited.
"""
from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
import shutil
from urllib.parse import quote


def build(legacy_root: Path, output: Path, repository: Path) -> None:
    publications = json.loads((repository / "website/publications.json").read_text(encoding="utf-8"))
    index = (legacy_root / "replay/index.html").read_text(encoding="utf-8")
    anchor = '<section id="replay-workspace"'
    if index.count(anchor) != 1:
        raise ValueError("Expected the frozen replay workspace exactly once")
    if output.exists():
        raise FileExistsError("Use a fresh output directory; existing sites are not overwritten")

    cards = []
    copies = []
    for paper in publications:
        source = (repository / paper["source"]).resolve()
        source.relative_to(repository.resolve())
        destination = Path(paper["destination"])
        if destination.is_absolute() or ".." in destination.parts:
            raise ValueError("Publication destination must stay inside the site")
        with source.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError(f"Not a PDF: {paper['source']}")
        href = escape(quote(destination.as_posix(), safe="/"), quote=True)
        cards.append(
            '<article class="run-hero panel-accent">'
            '<div class="hero-copy"><p class="section-kicker">新增综述成果</p>'
            f'<h2>{escape(paper["title"])}</h2>'
            f'<p class="question">作者：{escape(paper["author"])} · 日期：{escape(paper["date"])}</p></div>'
            '<div class="hero-result"><span>综述全文</span><div class="hero-links">'
            f'<a href="{href}" target="_blank" rel="noreferrer">在线阅读 PDF</a>'
            f'<a href="{href}" download>下载 PDF</a></div></div></article>'
        )
        copies.append((source, destination))

    shutil.copytree(legacy_root / "replay", output)
    shutil.copytree(legacy_root / "artifacts/e2e", output / "artifacts/e2e")
    for source, destination in copies:
        target = output / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    additions = '<section class="section-block" aria-label="新增综述成果">' + "\n".join(cards) + '</section>\n        '
    (output / "index.html").write_text(index.replace(anchor, additions + anchor, 1), encoding="utf-8")
    (output / ".nojekyll").touch()
    print(f"Preserved frozen replay; added {len(copies)} publication(s) to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.legacy_root, args.output, Path(__file__).resolve().parents[2])
