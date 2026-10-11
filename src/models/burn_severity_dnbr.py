"""Uniform dNBR thresholds for burn area and severity."""

from __future__ import annotations

import numpy as np

from src.models.common import remove_small_components


def classify(features: dict[str, np.ndarray], threshold: np.ndarray | float, params: dict) -> np.ndarray:
    dnbr = features["dnbr"]
    burn = features["valid"] & (dnbr >= threshold)
    burn = remove_small_components(burn, int(params.get("min_component_px", 1)))
    severity = np.zeros(dnbr.shape, dtype=np.uint8)
    severity[burn] = 1
    severity[burn & (dnbr >= float(params["severity_2"]))] = 2
    severity[burn & (dnbr >= float(params["severity_3"]))] = 3
    return severity


def predict(features: dict[str, np.ndarray], params: dict) -> np.ndarray:
    return classify(features, float(params["dnbr_min"]), params)
