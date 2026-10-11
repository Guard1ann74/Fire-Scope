"""Explicit model registry and required preprocessing features."""

from __future__ import annotations

from collections.abc import Callable

from src.models.burn_severity_calibrated_dnbr import predict as burn_severity_calibrated_dnbr
from src.models.burn_severity_dnbr import predict as burn_severity_dnbr
from src.models.burn_severity_landcover_contour_dnbr import predict as burn_severity_landcover_contour_dnbr
from src.models.burn_severity_landcover_dnbr import predict as burn_severity_landcover_dnbr


MODELS: dict[str, dict[str, tuple[Callable, frozenset[str]]]] = {
    "burn_severity": {
        "dnbr_threshold": (burn_severity_dnbr, frozenset({"dnbr", "valid"})),
        "landcover_dnbr": (burn_severity_landcover_dnbr, frozenset({"dnbr", "valid", "landcover"})),
        "calibrated_dnbr": (burn_severity_calibrated_dnbr, frozenset({"dnbr", "valid", "landcover"})),
        "landcover_contour_dnbr": (burn_severity_landcover_contour_dnbr, frozenset({"dnbr", "valid", "landcover"})),
    },
}
