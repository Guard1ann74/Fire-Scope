"""Place-search response mapping and API behavior."""

import io
import json
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.app import create_app
from service.config import Settings
from service.geocode import PhotonSearch, _place


class GeocodeTests(TestCase):
    def test_photon_extent_becomes_leaflet_bounds(self):
        place = _place({
            "properties": {"name": "Красноярск", "state": "Красноярский край", "country": "Россия",
                           "type": "city", "extent": [92.6, 56.2, 93.2, 55.9]},
            "geometry": {"coordinates": [92.9, 56.0]},
        })
        self.assertEqual(place["bbox"], [92.6, 55.9, 93.2, 56.2])
        self.assertEqual(place["detail"], "Красноярский край, Россия")

    def test_identical_search_is_cached(self):
        payload = {"features": [{"properties": {"name": "Париж", "type": "city"},
                                 "geometry": {"coordinates": [2.35, 48.86]}}]}
        with patch("service.geocode.REQUEST_INTERVAL", 0), patch("service.geocode.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
            search = PhotonSearch()
            self.assertEqual(search.search("Париж"), search.search(" париж "))
            self.assertEqual(urlopen.call_count, 1)

    def test_places_api_returns_geocoder_results(self):
        app = create_app(Settings())
        with patch.object(app.state.geocoder, "search", return_value=[{"name": "Красноярск"}]) as search:
            response = TestClient(app).get("/api/places", params={"q": "Красноярск"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["places"][0]["name"], "Красноярск")
        search.assert_called_once_with("Красноярск")
