"""Мониторинг: новые очаги из FIRMS становятся оповещениями, повторы не плодятся (без сети)."""

from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from fastapi.testclient import TestClient
from shapely.geometry import box

from service.app import create_app
from service.config import Settings
from service.fire_evidence import FirePoint, PersistentSources
from service.monitor import FireMonitor
from tests.test_fire_first import FakeArchive, predictor, FakeBackend

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
ZONE = box(38.80, 45.00, 39.10, 45.20)


def fire(lon, lat, hhmm="1013", day=date(2026, 10, 11), confidence="nominal", frp=12.0, daynight="D"):
    return FirePoint(lon, lat, day, hhmm, "VIIRS_SNPP_NRT", "Suomi NPP", confidence, frp, None, daynight)


class Archive(FakeArchive):
    def set(self, points):
        self._points = points


def make(directory, points, clock=lambda: NOW):
    archive = Archive(points)
    instance = predictor(archive, FakeBackend())
    config = {**instance.config["monitor"]}
    return FireMonitor(Path(directory) / "state.json", instance, config, clock=clock), archive


class MonitorTests(TestCase):
    def test_new_fire_becomes_alert_and_is_not_repeated(self):
        with TemporaryDirectory() as directory:
            monitor, _ = make(directory, [fire(38.95, 45.10), fire(38.951, 45.101, "1015")])
            zone = monitor.add_zone("Тест", ZONE)
            first = monitor.check_zone(zone["id"])
            again = monitor.check_zone(zone["id"])
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["n_points"], 2)
            self.assertFalse(first[0]["read"])
            self.assertEqual(again, [])
            self.assertEqual(monitor.summary()["unread"], 1)

    def test_filters_and_area_apply(self):
        with TemporaryDirectory() as directory:
            points = [fire(38.95, 45.10, confidence="low"),        # низкая достоверность
                      fire(40.50, 45.10),                          # вне района
                      fire(38.95, 45.10, day=date(2026, 10, 9))]   # старше окна
            monitor, _ = make(directory, points)
            zone = monitor.add_zone("Тест", ZONE)
            self.assertEqual(monitor.check_zone(zone["id"]), [])
            check = monitor.zones()[0]["last_check"]
            self.assertEqual((check["points_total"], check["points_kept"]), (1, 0))

    def test_new_points_near_recent_fire_update_it(self):
        with TemporaryDirectory() as directory:
            clock = {"now": NOW}
            monitor, archive = make(directory, [fire(38.95, 45.10)], clock=lambda: clock["now"])
            zone = monitor.add_zone("Тест", ZONE)
            monitor.check_zone(zone["id"])
            monitor.mark_read()
            clock["now"] = NOW + timedelta(hours=3)
            archive.set([fire(38.95, 45.10), fire(38.96, 45.10, "1410")])
            changed = monitor.check_zone(zone["id"])
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0]["updates"], 1)
            self.assertEqual(changed[0]["n_points"], 2)
            self.assertEqual(len(monitor.alerts()), 1)
            self.assertEqual(monitor.summary()["unread"], 1)

    def test_firms_failure_is_reported_not_hidden(self):
        with TemporaryDirectory() as directory:
            monitor, _ = make(directory, [])
            monitor.predictor.archive = FakeArchive(error="HTTP 503")
            zone = monitor.add_zone("Тест", ZONE)
            self.assertEqual(monitor.check_zone(zone["id"]), [])
            check = monitor.zones()[0]["last_check"]
            self.assertFalse(check["ok"])
            self.assertIn("FIRMS", check["error"])

    def test_state_survives_restart_and_zone_size_is_limited(self):
        with TemporaryDirectory() as directory:
            monitor, _ = make(directory, [fire(38.95, 45.10)])
            zone = monitor.add_zone("Тест", ZONE)
            monitor.check_zone(zone["id"])
            reopened, _ = make(directory, [])
            self.assertEqual(len(reopened.alerts()), 1)
            with self.assertRaises(ValueError):
                monitor.add_zone("Огромный", box(30, 40, 45, 50))


class MonitorApiTests(TestCase):
    def test_zone_check_and_read_through_api(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            app = create_app(Settings(db_path=f"{directory}/db.sqlite3", cdse_client_id="id",
                                      cdse_client_secret="secret", firms_map_key="key",
                                      satellite_cache_dir=Path(directory) / "satellite-cache"))
            today = datetime.now(timezone.utc)
            app.state.monitor.predictor.archive = FakeArchive(
                [fire(38.95, 45.10, today.strftime("%H%M"), today.date())])
            app.state.monitor.predictor.persistent = PersistentSources([], 500)
            app.state.monitor.weather = None
            client = clients.enter_context(TestClient(app))
            added = client.post("/api/monitor/zones", json={"name": "Тест", "bbox": list(ZONE.bounds)}).json()
            state = client.get("/api/monitor").json()
            read = client.post("/api/monitor/alerts/read").json()
            removed = client.delete(f"/api/monitor/zones/{added['zone']['id']}").status_code
            after = client.get("/api/monitor").json()
        self.assertEqual(len(added["alerts"]), 1)
        self.assertEqual(state["unread"], 1)
        self.assertFalse(state["running"])  # в тестах фоновый опрос выключен
        self.assertEqual(read["changed"], 1)
        self.assertEqual(removed, 200)
        self.assertEqual((after["zones"], after["alerts"]), ([], []))


class MonitorDedupTests(TestCase):
    def test_close_clusters_of_one_check_are_one_alert(self):
        with TemporaryDirectory() as directory:
            # Две группы в 1.5 км: разные кластеры (связь 1 км), но одно оповещение (слияние 2 км).
            monitor, _ = make(directory, [fire(38.95, 45.10), fire(38.969, 45.10)])
            zone = monitor.add_zone("Тест", ZONE)
            changed = monitor.check_zone(zone["id"])
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0]["updates"], 0)
            self.assertEqual(changed[0]["n_points"], 2)

    def test_alert_gets_weather_and_survives_weather_failure(self):
        from service.weather import WeatherError

        class FakeWeather:
            fail = False

            def at(self, lon, lat, when):
                if self.fail:
                    raise WeatherError("Open-Meteo недоступен")
                return {"wind_from": "СВ", "danger_class": 3, "at": when.isoformat()}

        with TemporaryDirectory() as directory:
            monitor, archive = make(directory, [fire(38.95, 45.10)])
            monitor.weather = FakeWeather()
            zone = monitor.add_zone("Тест", ZONE)
            alert = monitor.check_zone(zone["id"])[0]
            self.assertEqual(alert["weather"]["danger_class"], 3)
            self.assertEqual(monitor.alerts()[0]["weather"]["wind_from"], "СВ")
            monitor.weather.fail = True
            archive.set([fire(38.95, 45.10), fire(38.951, 45.10, "1130")])
            updated = monitor.check_zone(zone["id"])[0]
            self.assertEqual(updated["n_points"], 2)  # оповещение есть и без погоды
            self.assertIn("error", monitor.alerts()[0]["weather"])
