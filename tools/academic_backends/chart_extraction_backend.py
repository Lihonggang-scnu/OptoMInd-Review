"""Chart/Spectrum extraction backends — audit and skeleton only.

Optical thin-film papers contain T(λ), R(λ), A(λ), emissivity(λ) plots.
Extracting numerical data from these plots is a key capability for OptoMind.

Tools audited:
- WebPlotDigitizer: Online + Electron app for manual digitization
- Plot2Spectra: Python-based spectral curve extractor (custom)
- ChartOCR: Vision-based chart data extraction
- Auto-trace: OpenCV-based curve tracing

Current status: SKELETON — no large models downloaded.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class ChartExtractionBackend:
    """Skeleton for chart/spectrum curve extraction."""

    def __init__(self) -> None:
        self.available = False
        self.tools: Dict[str, Dict[str, Any]] = {
            "webplotdigitizer": {
                "type": "electron_app",
                "url": "https://automeris.io/WebPlotDigitizer/",
                "install_status": "not_installed",
                "recommended_for": "manual digitization of T(λ)/R(λ) curves from paper figures",
                "note": "Best used as a manual tool for curating ground-truth spectra. Automatable via Electron CLI.",
            },
            "plot_digitizer_python": {
                "type": "python_library",
                "package": "plotdigitizer",
                "install_command": "py -3.11 -m pip install plotdigitizer",
                "install_status": "not_installed",
                "note": "Lightweight Python library for digitizing line/scatter plots.",
            },
            "chart_ocr": {
                "type": "research_project",
                "status": "not_integrated",
                "note": "Vision-based chart data extraction. Requires GPU and model weights. Not for Phase 1.",
            },
            "opencv_curve_trace": {
                "type": "custom_script",
                "status": "not_implemented",
                "note": "Can be built with OpenCV edge detection + color thresholding for spectral curves.",
            },
        }

    def install_plotdigitizer(self) -> bool:
        """Attempt to install plotdigitizer."""
        try:
            import subprocess, sys
            subprocess.check_call([sys.executable, "-m", "pip", "install", "plotdigitizer"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.tools["plot_digitizer_python"]["install_status"] = "installed"
            return True
        except Exception:
            return False

    def check_status(self) -> Dict[str, Any]:
        try:
            import importlib
            importlib.import_module("plotdigitizer")
            self.tools["plot_digitizer_python"]["install_status"] = "installed"
        except ImportError:
            pass
        return {"tools": self.tools}
