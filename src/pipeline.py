"""Run the preprocessing and model selected in the burn model config for a chip."""

from __future__ import annotations

import numpy as np

from src.dataset.chips import Chip
from src.models.registry import MODELS
from src.preprocessing.registry import PREPROCESSORS


def validate_selection(kind: str, settings: dict) -> None:
    if kind not in PREPROCESSORS or kind not in MODELS:
        raise ValueError(f"Unknown task kind: {kind}")
    if not isinstance(settings, dict):
        raise ValueError(f"{kind} settings must be a mapping")
    for selector, description, registry in (
        ("preprocessing", "preprocessing_models", PREPROCESSORS),
        ("model", "model_description", MODELS),
    ):
        name = settings.get(selector)
        if not isinstance(name, str) or name not in registry[kind]:
            choices = ", ".join(sorted(registry[kind]))
            raise ValueError(f"Unknown {kind} {selector} {name!r}; choose: {choices}")
        variants = settings.get(description)
        if not isinstance(variants, dict) or name not in variants:
            raise ValueError(f"{kind}.{description} must contain settings for selected {name!r}")
        for variant_name, params in variants.items():
            if variant_name not in registry[kind]:
                raise ValueError(f"Unknown {kind} {description} variant {variant_name!r}")
            if not isinstance(params, dict):
                raise ValueError(f"{kind}.{description}.{variant_name} must be a parameter mapping")
    prep_name = settings["preprocessing"]
    model_name = settings["model"]
    available = PREPROCESSORS[kind][prep_name][1]
    required = MODELS[kind][model_name][1]
    if missing := required - available:
        raise ValueError(f"{kind} model {model_name} needs features absent from {prep_name}: {sorted(missing)}")


def predict(chip: Chip, settings: dict) -> np.ndarray:
    validate_selection(chip.kind, settings)
    preprocessing_name = settings["preprocessing"]
    model_name = settings["model"]
    preprocessor = PREPROCESSORS[chip.kind][preprocessing_name][0]
    model = MODELS[chip.kind][model_name][0]
    features = preprocessor(chip, settings["preprocessing_models"][preprocessing_name])
    result = model(features, settings["model_description"][model_name])
    if result.shape != (chip.height, chip.width) or result.dtype != np.uint8:
        raise ValueError(f"Invalid prediction shape or type for {chip.chip_id}")
    if np.max(result) > 3:
        raise ValueError(f"Invalid class value for {chip.chip_id}")
    return result
