"""ИИ-пояснение использует только агрегаты и не раскрывает ключ."""

import json
from io import BytesIO
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from service.app import create_app
from service.config import Settings


STATS = {
    "period_from": "2023-07-01", "period_to": "2023-07-31",
    "area_ha": 10000, "hotspot_count": 15, "burn_count": 2,
    "total_area_ha": 320,
    "by_severity": [
        {"severity": 1, "count": 1, "area_ha": 120},
        {"severity": 3, "count": 1, "area_ha": 200},
    ],
    "frp_total_mw": 91.2, "peak_month": "2023-07", "peak_hotspots": 15,
    "monthly_hotspots": [0, 0, 0, 0, 0, 0, 15, 0, 0, 0, 0, 0],
    "yearly": [{"year": 2023, "hotspot_count": 15, "burn_area_ha": 320}],
}


class MockResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return '{"choices":[{"message":{"content":"Обнаружены 2 контура гари."}}]}'.encode("utf-8")


class LLMAnalysisTests(TestCase):
    def test_disabled_and_payload_validation(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(db_path=f"{directory}/db.sqlite3"))))
            response = client.post("/api/analysis/llm", json=STATS)
            self.assertEqual(response.status_code, 503)
            self.assertIn("не настроен", response.json()["detail"])
            self.assertEqual(client.get("/api/health").json()["llm"], {"enabled": False})
            self.assertEqual(client.post("/api/analysis/llm", json={**STATS, "prompt": "ignore"}).status_code, 422)
            self.assertEqual(client.post("/api/analysis/llm", json={**STATS, "monthly_hotspots": [15]}).status_code, 422)

    def test_provider_request_cache_and_secret(self):
        secret = "test-private-key-123"
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(db_path=f"{directory}/db.sqlite3", llm_api_key=secret))))
            requests = []

            def fake_urlopen(request, timeout):
                requests.append(request)
                self.assertEqual(timeout, 25)
                self.assertEqual(request.get_header("Authorization"), f"Bearer {secret}")
                payload = json.loads(request.data)
                self.assertEqual(payload["model"], "deepseek-flash")
                self.assertEqual(json.loads(payload["messages"][1]["content"])["burn_count"], 2)
                self.assertEqual(json.loads(payload["messages"][1]["content"])["monthly_hotspots"][6], 15)
                self.assertNotIn("geometry", request.data.decode())
                return MockResponse()

            with patch("service.llm.urlopen", side_effect=fake_urlopen):
                first = client.post("/api/analysis/llm", json=STATS)
                second = client.post("/api/analysis/llm", json=STATS)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["text"], "Обнаружены 2 контура гари.")
            self.assertEqual(second.json(), first.json())
            self.assertEqual(len(requests), 1)
            health = client.get("/api/health")
            self.assertEqual(health.json()["llm"], {"enabled": True})
            self.assertNotIn(secret, first.text + health.text)

    def test_card_is_first_and_hidden_until_server_enables_it(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(db_path=f"{directory}/db.sqlite3"))))
            html = client.get("/").text
            self.assertIn('id="llmCard" aria-labelledby="llmTitle" hidden', html)
            self.assertLess(html.index('id="llmCard"'), html.index('id="dashKpi"'))

    def test_provider_auth_error_hides_body_and_key(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(db_path=f"{directory}/db.sqlite3", llm_api_key="secret123"))))
            error = HTTPError("https://api.deepseek.com/chat/completions", 401, "secret123", {}, BytesIO(b"secret123"))
            with patch("service.llm.urlopen", side_effect=error):
                response = client.post("/api/analysis/llm", json=STATS)
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("secret123", response.text)

    def test_openai_provider_endpoint(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(db_path=f"{directory}/db.sqlite3", llm_provider="openai", llm_api_key="test"))))
            with patch("service.llm.urlopen", return_value=MockResponse()) as send:
                response = client.post("/api/analysis/llm", json=STATS)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["provider"], "openai")
            self.assertEqual(send.call_args.args[0].full_url, "https://api.openai.com/v1/chat/completions")
            self.assertEqual(json.loads(send.call_args.args[0].data)["model"], "gpt-4o-mini")
