"""Burn severity index calculation and optional Sentinel-2 SCL screening."""

from __future__ import annotations

import numpy as np

from src.dataset.chips import Chip


def optical_no_aux(chip: Chip, params: dict) -> dict[str, np.ndarray]:
    pre = chip.raster("sentinel2_pre")
    post = chip.raster("sentinel2_post")
    if pre.shape != (chip.height, chip.width, 10) or post.shape != pre.shape:
        raise ValueError(f"Unexpected Sentinel-2 shape for {chip.chip_id}")
    pre_nir, pre_swir = pre[..., 6].astype(np.float32), pre[..., 8].astype(np.float32)
    post_nir, post_swir = post[..., 6].astype(np.float32), post[..., 8].astype(np.float32)
    nbr_pre = (pre_nir - pre_swir) / np.maximum(pre_nir + pre_swir, 1.0)
    nbr_post = (post_nir - post_swir) / np.maximum(post_nir + post_swir, 1.0)
    valid = (pre_nir + pre_swir > 0) & (post_nir + post_swir > 0)
    return {
        "dnbr": nbr_pre - nbr_post,
        "valid": valid,
        "scl_pre": pre[..., 9],
        "scl_post": post[..., 9],
    }


def optical(chip: Chip, params: dict) -> dict[str, np.ndarray]:
    features = optical_no_aux(chip, params)
    aux = chip.raster("aux")
    if aux.shape != (chip.height, chip.width, 3):
        raise ValueError(f"Unexpected auxiliary shape for {chip.chip_id}")
    features["landcover"] = aux[..., 2]
    return features


def optical_scl_no_aux(chip: Chip, params: dict) -> dict[str, np.ndarray]:
    features = optical_no_aux(chip, params)
    excluded_scl = params.get("excluded_scl", [0, 1, 3, 8, 9, 10, 11])
    invalid = np.isin(features["scl_pre"], excluded_scl)
    invalid |= np.isin(features["scl_post"], excluded_scl)
    features["valid"] &= ~invalid
    return features


def optical_scl(chip: Chip, params: dict) -> dict[str, np.ndarray]:
    features = optical_scl_no_aux(chip, params)
    aux = chip.raster("aux")
    if aux.shape != (chip.height, chip.width, 3):
        raise ValueError(f"Unexpected auxiliary shape for {chip.chip_id}")
    features["landcover"] = aux[..., 2]
    excluded_landcover = params.get("excluded_landcover", [50, 80])
    features["valid"] &= ~np.isin(features["landcover"], excluded_landcover)
    return features


def optical_case(chip: Chip, params: dict) -> dict[str, np.ndarray]:
    """Exact dNBR and SCL screening used by calibrated_dnbr."""
    pre, post, aux = chip.raster("sentinel2_pre"), chip.raster("sentinel2_post"), chip.raster("aux")
    if pre.shape != (chip.height, chip.width, 10) or post.shape != pre.shape or aux.shape != (chip.height, chip.width, 3):
        raise ValueError(f"Unexpected burn severity raster shape for {chip.chip_id}")

    def nbr(image: np.ndarray) -> np.ndarray:
        nir, swir = image[..., 6].astype(np.float32), image[..., 8].astype(np.float32)
        denominator = nir + swir
        return np.where(denominator > 0, (nir - swir) / np.maximum(denominator, 1e-6), 0.0)

    invalid = np.isin(pre[..., 9], (0, 1, 3, 8, 9, 10))
    invalid |= np.isin(post[..., 9], (0, 1, 3, 8, 9, 10))
    return {"dnbr": nbr(pre) - nbr(post), "valid": ~invalid, "landcover": aux[..., 2]}
