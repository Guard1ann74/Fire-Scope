"""Read both chip types while translating the organizer's short directory names."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile


DATA_KIND = {"active_fire": "af", "burn_severity": "bs"}


@dataclass(frozen=True)
class Chip:
    chip_id: str
    kind: str
    width: int
    height: int
    root: Path
    meta: dict[str, str]

    def raster(self, folder: str) -> np.ndarray:
        directory = self.root / folder
        matches = sorted(directory.glob(f"{self.chip_id}_*.tif"))
        if len(matches) != 1:
            raise FileNotFoundError(f"Expected one {folder} raster for {self.chip_id}, found {len(matches)}")
        return tifffile.imread(matches[0])

    def mask(self) -> np.ndarray:
        return self.raster("masks")
