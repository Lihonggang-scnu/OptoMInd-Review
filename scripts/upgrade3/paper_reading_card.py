"""CLI entry point for the standalone per-paper A/B reading card."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.paper_reading_card import main


if __name__ == "__main__":
    raise SystemExit(main())
