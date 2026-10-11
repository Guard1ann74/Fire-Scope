"""The landcover dNBR rule keeps severity classes while cleaning the contour."""

from pathlib import Path
import unittest

import numpy as np

from src.config import load_config
from src.models.burn_severity_landcover_contour_dnbr import predict
from src.pipeline import validate_selection


class LandcoverContourDnbrTests(unittest.TestCase):
    def test_contour_is_independent_of_severity_thresholds(self):
        features = {
            "dnbr": np.array([[.06, .07, .08, .22, .40, .15]], np.float32),
            "landcover": np.array([[30, 30, 30, 30, 30, 10]], np.uint8),
            "valid": np.ones((1, 6), bool),
        }
        result = predict(features, {"scale": 1.0, "contour_scale": 1.2, "min_component_px": 1})
        np.testing.assert_array_equal(result, [[0, 0, 1, 2, 3, 1]])
        features["valid"][0, 5] = False
        self.assertEqual(predict(features, {"min_component_px": 1})[0, 5], 0)

    def test_small_components_are_removed_without_changing_large_ones(self):
        features = {
            "dnbr": np.zeros((40, 40), np.float32),
            "landcover": np.full((40, 40), 30, np.uint8),
            "valid": np.ones((40, 40), bool),
        }
        features["dnbr"][:35, :35] = .22  # 1225 pixels, class 2
        features["dnbr"][39, 39] = .40  # isolated class 3
        result = predict(features, {"min_component_px": 1000})
        self.assertEqual(int(np.count_nonzero(result)), 1225)
        self.assertEqual(int(result[0, 0]), 2)
        self.assertEqual(int(result[39, 39]), 0)

    def test_model_is_selectable_with_branch_matching_preprocessing(self):
        config = Path(__file__).resolve().parents[1] / "configs" / "burn_model.yaml"
        settings = load_config(config)["burn_severity"].copy()
        settings["preprocessing"] = "optical_case"
        settings["model"] = "landcover_contour_dnbr"
        validate_selection("burn_severity", settings)


if __name__ == "__main__":
    unittest.main()
