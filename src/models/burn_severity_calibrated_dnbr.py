"""Land-cover-specific dNBR boundaries from the module1-training baseline."""

from __future__ import annotations

import numpy as np


THRESHOLDS = {
    10: (0.100, 0.270, 0.660),
    20: (0.100, 0.270, 0.660),
    30: (0.062, 0.204, 0.386),
    40: (0.070, 0.177, 0.380),
    90: (0.075, 0.331, 0.677),
}
DEFAULT = (0.070, 0.204, 0.400)


def predict(features: dict[str, np.ndarray], params: dict) -> np.ndarray:
    dnbr, landcover = features["dnbr"], features["landcover"]
    scale = float(params.get("scale", 1.0))
    if scale <= 0:
        raise ValueError("scale must be positive")
    boundaries = [np.full(dnbr.shape, value * scale, dtype=np.float32) for value in DEFAULT]
    for cls, values in THRESHOLDS.items():
        where = landcover == cls
        for threshold, value in zip(boundaries, values):
            threshold[where] = value * scale
    result = np.zeros(dnbr.shape, dtype=np.uint8)
    for severity, threshold in enumerate(boundaries, 1):
        result[dnbr >= threshold] = severity
    result[~features["valid"]] = 0
    return result
