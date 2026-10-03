"""Offline reproduction of the observed WO03 local selection boundary."""

import json
from pathlib import Path

fixture = json.loads((Path(__file__).with_name("LOCAL_SELECTION_FIXTURE.json")).read_text(encoding="utf-8"))
ranks = fixture["observed_default_ranking"]
top6 = [row["source_handle"] for row in ranks if row["rank"] <= fixture["stored_gap"]["default_top_papers"]]
print("default_top_papers=6:", top6)
print("P0004 selected:", "P0004" in top6)
print("rank of P0004:", next(row["rank"] for row in ranks if row["source_handle"] == "P0004"))
print("known_handles_behavior:", fixture["selection_effect"]["known_handles_behavior"])
