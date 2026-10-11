"""Land-cover dNBR thresholds with an independent contour and area filter."""

from __future__ import annotations

import numpy as np

from src.models.burn_severity_calibrated_dnbr import DEFAULT, THRESHOLDS, predict as calibrated_predict
from src.models.common import remove_small_components


def predict(features: dict[str, np.ndarray], params: dict) -> np.ndarray:
    scale = float(params.get("scale", 1.0))
    contour_scale = float(params.get("contour_scale", 1.2))
    minimum = int(params.get("min_component_px", 1000))
    if scale <= 0 or contour_scale <= 0 or minimum < 1:
        raise ValueError("scale and contour_scale must be positive; min_component_px must be >= 1")

    result = calibrated_predict(features, {"scale": scale})
    landcover = features["landcover"]
    contour_base = np.full(features["dnbr"].shape, DEFAULT[0], dtype=np.float32)
    for cls, thresholds in THRESHOLDS.items():
        contour_base[landcover == cls] = thresholds[0]
    result[features["dnbr"] < contour_base * contour_scale] = 0
    result[~remove_small_components(result > 0, minimum)] = 0
    return result
