"""Configuration of the burn model: preprocessing and model selection with their parameters."""

from __future__ import annotations

from pathlib import Path
import yaml

from src.pipeline import validate_selection


def load_config(path: str | Path) -> dict:
    config_path = Path(path).expanduser().resolve()
    with config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict) or "burn_severity" not in config:
        raise ValueError("Config must contain a burn_severity section")
    validate_selection("burn_severity", config["burn_severity"])
    return config
