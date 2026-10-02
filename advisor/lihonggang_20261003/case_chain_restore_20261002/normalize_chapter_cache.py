from __future__ import annotations

import json
import shutil
from pathlib import Path

root = Path(__file__).resolve().parent
chapter_dir = root / "stages" / "chapters"
backup = root / "observations" / "pre_normalization_chapters"
backup.mkdir(parents=True, exist_ok=True)
changed = {}
for path in sorted(chapter_dir.glob("CH[0-9][0-9].json")):
    shutil.copy2(path, backup / path.name)
    packet = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = packet.get("source_materials") or []
    kept = [
        row for row in rows
        if not (isinstance(row, dict) and row.get("source_role") == "candidate_navigation")
    ]
    packet["source_materials"] = kept
    path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    changed[path.name] = {
        "before": len(rows),
        "after": len(kept),
        "candidate_rows_kept_in_candidate_materials": len(rows) - len(kept),
    }
result = {
    "status": "normalized_isolated_copies_only",
    "reason": "candidate_navigation rows were duplicated into source_materials by the prior packet writer",
    "backup_dir": str(backup),
    "changed": changed,
}
(root / "observations" / "CACHE_NORMALIZATION.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=False, indent=2))
