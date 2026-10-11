"""Маска постоянных источников: лежит в репозитории, отсутствующая строится при запуске (без сети)."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from service.config import PROJECT_ROOT
from service.fire_evidence import PersistentSources, load_service_config
from service.persistent_mask import MaskBootstrap


class PersistentMaskTests(TestCase):
    def test_mask_for_the_region_is_in_the_repository(self):
        config = load_service_config(PROJECT_ROOT / "configs" / "service.yaml")
        files = [PROJECT_ROOT / path for path in config["persistent_sources"]["files"]]
        self.assertTrue(all(path.is_file() for path in files))
        sources = PersistentSources(files, config["persistent_sources"]["radius_m"])
        self.assertTrue(sources.coverage)
        self.assertTrue(sources.loaded[0].startswith("configs/persistent-sources/"))

    def test_missing_mask_without_key_is_reported(self):
        with TemporaryDirectory() as directory:
            bootstrap = MaskBootstrap([Path(directory) / "mask.geojson"], None, {}, "", Path(directory), lambda: None)
            bootstrap.start()
        self.assertEqual(bootstrap.state["status"], "missing")
        self.assertIn("FIRMS_MAP_KEY", bootstrap.state["detail"])

    def test_missing_mask_is_built_and_picked_up(self):
        ready = []

        def fake_build(region, output, config, key, cache, end=None):
            Path(output).write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
            return {"persistent_sources": 0}

        with TemporaryDirectory() as directory:
            target = Path(directory) / "mask.geojson"
            region = PROJECT_ROOT / "configs" / "regions" / "krasnodar_adygea.geojson"
            with patch("service.persistent_mask.build_mask", fake_build):
                bootstrap = MaskBootstrap([target], region, {}, "key", Path(directory), lambda: ready.append(True))
                bootstrap.start()
                bootstrap._thread.join(timeout=10)
            self.assertTrue(target.is_file())
        self.assertEqual(bootstrap.state["status"], "ready")
        self.assertEqual(ready, [True])
