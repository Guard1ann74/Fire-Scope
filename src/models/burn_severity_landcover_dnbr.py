"""Land-cover-adjusted dNBR threshold model."""

from __future__ import annotations

import numpy as np

from src.models.burn_severity_dnbr import classify


def predict(features: dict[str, np.ndarray], params: dict) -> np.ndarray:
    floor = float(params["dnbr_min"])
    lower_classes = params.get("lower_threshold_landcover", [30, 40, 90])
    lower_factor = float(params.get("lower_threshold_factor", 0.8))
    threshold = np.where(np.isin(features["landcover"], lower_classes), floor * lower_factor, floor)
    return classify(features, threshold, params)
