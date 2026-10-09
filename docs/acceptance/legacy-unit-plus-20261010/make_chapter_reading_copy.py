"""Build a free, chapter-scoped reading copy from this run's completed units."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--chapter", required=True)
    args = parser.parse_args()
    root = Path(args.run_root).resolve()
    chapter = args.chapter
    unit_root = root / "units" / chapter
    out_root = root / "chapter_reading"
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for result_path in sorted(unit_root.glob("**/UNIT_RESULT.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not result.get("complete") or not result.get("body_path"):
            continue
        body_path = Path(result["body_path"])
        if not body_path.is_file():
            continue
        body = body_path.read_text(encoding="utf-8")
        rows.append({
            "chapter_id": result.get("chapter_id", chapter),
            "unit_id": result.get("unit_id"),
            "attempt_dir": str(result_path.parent),
            "body_path": str(body_path),
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "body_characters": len(body),
            "issues": result.get("issues", []),
            "body": body,
        })
    rows.sort(key=lambda row: str(row["unit_id"]))
    copy_path = out_root / f"{chapter}_BODY_READING_COPY.md"
    chunks = [
        f"# Free reading copy — {chapter}\n\n",
        "This file contains only completed unit bodies generated in this live run. "
        "Unit boundaries are preserved; it is not a replacement manuscript.\n\n",
    ]
    for row in rows:
        chunks.append(f"---\n\n## {row['chapter_id']} / {row['unit_id']}\n\n")
        chunks.append(row["body"].rstrip() + "\n\n")
    copy_path.write_text("".join(chunks), encoding="utf-8")
    index = [{key: value for key, value in row.items() if key != "body"} for row in rows]
    (out_root / f"{chapter}_BODY_READING_INDEX.json").write_text(
        json.dumps({"chapter_id": chapter, "unit_count": len(index), "units": index},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"chapter_id": chapter, "unit_count": len(index),
                      "copy": str(copy_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
