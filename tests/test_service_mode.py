"""The service runs on real sources only: satellite by default, archive needs real files."""

from contextlib import ExitStack
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.__main__ import main as run_service
from service.app import create_app
from service.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "archive"


class ServiceModeTests(TestCase):
    def test_project_config_starts_on_real_satellite_data(self):
        with patch.dict(os.environ, {"FIRES_QUERY_MODE": "", "FIRES_DATA_DIR": ""}):
            settings = Settings.from_env()
        self.assertEqual(settings.query_mode, "satellite")
        self.assertIsNone(settings.data_dir)

    def test_missing_credentials_are_reported_not_replaced_with_data(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(
                db_path=f"{directory}/db.sqlite3", cdse_client_id="", cdse_client_secret=""))))
            health = client.get("/api/health").json()
            self.assertEqual(health["source"]["kind"], "satellite")
            self.assertFalse(health["prediction"]["enabled"])
            self.assertEqual(health["counts"], {"hotspots": 0, "burns": 0})
            response = client.get("/api/query", params={
                "bbox": "38.90,45.00,39.10,45.12", "from": "2024-08-01", "to": "2024-08-20"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("CDSE_CLIENT_ID", response.json()["detail"])

    def test_archive_mode_requires_a_real_directory(self):
        with self.assertRaises(ValueError):
            create_app(Settings(query_mode="archive", data_dir=None))
        with patch.dict(os.environ, {"FIRES_QUERY_MODE": "archive", "FIRES_DATA_DIR": ""}):
            self.assertEqual(run_service([]), 1)

    def test_archive_mode_serves_prepared_files(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(
                db_path=f"{directory}/db.sqlite3", query_mode="archive", data_dir=FIXTURE))))
            health = client.get("/api/health").json()
            self.assertEqual(health["source"]["kind"], "files")
            self.assertEqual(health["counts"], {"hotspots": 1, "burns": 1})
            self.assertFalse(health["live"]["enabled"])
            live = client.get("/api/live", params={"bbox": "38.90,45.00,39.10,45.12"}).json()
            self.assertFalse(live["available"])
            self.assertIn("отключены", live["error"])
            regions = {item["name"]: item for item in client.get("/api/regions").json()["regions"]}
            west, south, east, north = regions["Тестовый регион"]["bbox"]
            self.assertLess(west, east)
            self.assertLess(south, north)

    def test_satellite_mode_has_no_archive_region_suggestions(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(
                db_path=f"{directory}/db.sqlite3", query_mode="satellite"))))
            self.assertEqual(client.get("/api/regions").json(), {"regions": []})

    def test_yaml_flag_can_switch_to_archive_mode(self):
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.yaml"
            config.write_text("service:\n  use_external_apis: false\n", encoding="utf-8")
            with patch.dict(os.environ, {
                "FIRES_MODEL_CONFIG": str(config), "FIRES_QUERY_MODE": "",
            }):
                settings = Settings.from_env()
        self.assertEqual(settings.query_mode, "archive")

    def test_invalid_model_config_is_reported_before_any_download(self):
        from service.satellite import SatellitePredictor

        with TemporaryDirectory() as directory, ExitStack() as clients:
            config = Path(directory) / "broken.yaml"
            config.write_text(
                "burn_severity:\n  preprocessing: optical_case\n  model: no_such_model\n"
                "  preprocessing_models:\n    optical_case: {}\n"
                "  model_description:\n    no_such_model: {}\n"
                "service:\n  use_external_apis: true\n",
                encoding="utf-8")
            settings = Settings(db_path=f"{directory}/db.sqlite3", model_config=config,
                                cdse_client_id="id", cdse_client_secret="secret")
            self.assertIn("некорректна", SatellitePredictor(settings).model_problem())
            client = clients.enter_context(TestClient(create_app(settings)))
            prediction = client.get("/api/health").json()["prediction"]
            self.assertFalse(prediction["enabled"])
            self.assertIn("некорректна", prediction["detail"])
            response = client.get("/api/query", params={
                "bbox": "38.90,45.00,39.10,45.12", "from": "2024-08-01", "to": "2024-08-20"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("некорректна", response.json()["detail"])

    def test_default_service_model_needs_no_weights(self):
        from service.satellite import SatellitePredictor

        self.assertIsNone(SatellitePredictor(Settings()).model_problem())
