"""The map query must return model output for the selected area."""

from datetime import date
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from shapely.geometry import box

from service.app import create_app
from service.config import Settings
from service.query import QueryError, QuerySpec
from service.satellite import SatellitePredictor
from service.sources import Burn, Dataset


class SatelliteQueryTests(TestCase):
    def test_query_uses_prediction_and_includes_post_event_imagery(self):
        app = create_app(Settings(
            query_mode="satellite", cdse_client_id="test", cdse_client_secret="test",
        ))
        predicted = Dataset(
            hotspots=(),
            burns=(Burn("predicted-1", date(2024, 7, 20), 2,
                        box(42.01, 46.41, 42.02, 46.42), "Sentinel-2 / test-model"),),
            origin="test satellite", files=("post-scene",),
            updated_at="2024-07-20T12:00:00+00:00",
        )
        metadata = {"model": "test-model", "scenes": [{"pre_date": "2024-07-05",
                                                      "post_date": "2024-07-20"}]}
        with patch.object(app.state.predictor, "predict", return_value=(predicted, metadata)) as predict:
            response = TestClient(app).post("/api/query", json={
                "bbox": "42.0,46.4,42.03,46.43", "from": "2024-07-06", "to": "2024-07-19",
                "fire_first": False,
            })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["prediction"]["model"], "test-model")
        self.assertEqual(body["summary"]["burn_count"], 1)
        self.assertEqual(body["burns"]["features"][0]["properties"]["date"], "2024-07-20")
        predict.assert_called_once()

    def test_rejects_oversized_period_before_network_request(self):
        predictor = SatellitePredictor(Settings(
            query_mode="satellite", cdse_client_id="test", cdse_client_secret="test",
        ))
        spec = QuerySpec(box(42.0, 46.4, 42.01, 46.41),
                         date(2024, 1, 1), date(2024, 3, 1), "bbox")
        with self.assertRaisesRegex(QueryError, "31"):
            predictor.predict(spec)
