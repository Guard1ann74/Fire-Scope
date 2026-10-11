"""The service must never show synthetic or invented content."""

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from fastapi.testclient import TestClient

from service.app import create_app
from service.config import PACKAGE_ROOT, WEB_ROOT, Settings

FIXTURE = Path(__file__).parent / "fixtures" / "archive"
QUERY = {"bbox": "38.90,45.00,39.10,45.12", "from": "2024-08-01", "to": "2024-08-31"}


class HonestSourcesTests(TestCase):
    def test_no_bundled_demo_data(self):
        self.assertFalse((PACKAGE_ROOT / "data" / "mock").exists())
        self.assertFalse((PACKAGE_ROOT / "tools" / "make_mock.py").exists())

    def test_origin_reaches_query_and_exports(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(
                db_path=f"{directory}/db.sqlite3", query_mode="archive", data_dir=FIXTURE))))
            source = client.get("/api/query", params=QUERY).json()["data_source"]
            self.assertEqual(source["kind"], "files")
            self.assertIs(source["synthetic"], False)
            report = client.get("/api/report.json", params=QUERY).json()
            self.assertEqual(report["data_source"], source)
            burns = client.get("/api/export/burns.geojson", params=QUERY).json()
            self.assertEqual(burns["metadata"]["data_source"], source)
            self.assertIn("синтетические;нет", client.get("/api/report.csv", params=QUERY).text)

    def test_page_has_no_invented_content(self):
        page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        for marker in ("weather-demo", "news-demo", "mockNotice", "ДЕМО"):
            self.assertNotIn(marker, page)
        self.assertIn('id="sourceNotice"', page)
        # Поиск публикаций убран: без названия места запрос искал пожары по всей стране.
        self.assertFalse((WEB_ROOT / "news-search.js").exists())
        self.assertNotIn("news-search", page)


class UnobservedAreaTests(TestCase):
    def test_cloud_share_reaches_reports(self):
        from service.export import burns_geojson, report_csv, report_json

        hidden = {"type": "Polygon", "coordinates": [[[39.0, 45.0], [39.01, 45.0], [39.01, 45.01], [39.0, 45.0]]]}
        result = {
            "query": {"from": "2024-08-01", "to": "2024-08-20", "area_source": "bbox", "area_ha": 100.0,
                      "bbox": [39.0, 45.0, 39.02, 45.02], "metric_crs": "EPSG:32637"},
            "hotspots": {"type": "FeatureCollection", "features": []},
            "burns": {"type": "FeatureCollection", "features": []},
            "summary": {"hotspot_count": 0, "burn_count": 0, "total_area_ha": 0.0, "by_severity": [],
                        "satellites": [], "observed_range": {"from": None, "to": None},
                        "period": {"from": "2024-08-01", "to": "2024-08-20"}, "metric_crs": "EPSG:32637",
                        "empty": True, "generated_at": "2026-10-10T00:00:00+00:00"},
            "data_source": {"kind": "satellite", "synthetic": False, "description": "test"},
            "prediction": {"observed_fraction": 0.4, "unobserved": hidden},
        }
        report = report_json(result)
        self.assertEqual(report["observed_fraction"], 0.4)
        self.assertEqual(report["unobserved"], hidden)
        self.assertEqual(burns_geojson(result)["metadata"]["observed_fraction"], 0.4)
        self.assertIn("видимая_доля_участка", report_csv(result))

    def test_page_draws_hatched_cloud_layer(self):
        page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="unobservedHatch"', page)
        self.assertIn('id="layerUnobserved"', page)
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("unobserved-area", script)
        self.assertIn("prediction.unobserved", script)
