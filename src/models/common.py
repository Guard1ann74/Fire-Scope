"""Shared mask operations for simple baseline models."""

from __future__ import annotations

import numpy as np
from scipy.ndimage import label


def remove_small_components(mask: np.ndarray, minimum: int) -> np.ndarray:
    if minimum <= 1:
        return mask
    components, count = label(mask)
    sizes = np.bincount(components.ravel(), minlength=count + 1)
    return mask & (sizes[components] >= minimum)
