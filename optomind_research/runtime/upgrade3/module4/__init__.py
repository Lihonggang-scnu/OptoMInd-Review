"""Module 4: a source-preserving, full-available-text paper reader."""

from .contracts import (
    ContractError,
    Module4Blocked,
    assemble_reading_input,
    validate_input,
)
from .reader import read_paper
from .dossier import render_dossier, validate_dossier
from .feedback import build_feedback, build_post_reading_view

__all__ = [
    "ContractError", "Module4Blocked", "assemble_reading_input", "validate_input", "read_paper",
    "validate_dossier", "render_dossier", "build_feedback", "build_post_reading_view",
]
