"""Fire-first mode: burns only around confirmed fire, honest status otherwise (no network)."""

import atexit
from contextlib import ExitStack
import shutil
import tempfile
from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import numpy as np
from fastapi.testclient import TestClient
from shapely.geometry import box, shape

from service.app import create_app
from service.config import PROJECT_ROOT, Settings
from service.fire_evidence import (FirePoint, FireSourceError, PersistentSources, cluster_events,
                                   persistent_cells)
from service.fire_first import FireFirstPredictor
from service.query import QuerySpec

AREA = box(38.80, 45.00, 39.10, 45.20)
START, END = date(2024, 8, 1), date(2024, 8, 20)
FIRE = (38.95, 45.10)


def point(lon, lat, day, confidence="nominal", fire_type=0, source="VIIRS_SNPP_SP"):
    return FirePoint(lon, lat, day, "1030", source, "Suomi NPP", confidence, 12.5, fire_type)


class FakeArchive:
    enabled = True

    def __init__(self, points=None, error=None):
        self._points = points or []
        self._error = error
        self.calls = 0

    def points(self, bounds, start, end):
        self.calls += 1
        if self._error:
            raise FireSourceError(self._error)
        return list(self._points), ["VIIRS_SNPP_SP"], []


class FakeBackend:
    """Каталог из заданных дат; облачность SCL по дате; модель рисует гарь везде."""

    def __init__(self, cloudy=()):
        self.cloudy = {date.fromisoformat(d): fraction for d, fraction in dict(cloudy).items()}
        self.model_calls = []
        self.days = [date(2024, 7, 20), date(2024, 7, 28), date(2024, 8, 25), date(2024, 8, 30)]

    def catalogue(self, footprint, start, end):
        return [{"id": f"S2_{d.isoformat()}", "collection": "sentinel-2-l2a",
                 "properties": {"datetime": f"{d.isoformat()}T09:00:00Z"}}
                for d in self.days if start <= d <= end]

    def scl(self, bounds, when):
        # Облака равномерно по плитке: доля над любым буфером равна заданной.
        fraction = self.cloudy.get(when.date(), 0.0)
        cells = np.add.outer(np.arange(512), np.arange(512)) % 100
        return np.where(cells < round(fraction * 100), 9, 4).astype(np.uint16)

    def scene(self, bounds, when):
        data = np.full((10, 512, 512), 1000, np.uint16)
        data[0] = 100  # синий канал ясной погоды: без дымки
        data[9] = self.scl(bounds, when)
        return data

    def blue(self, bounds, when):
        # Тёмные объекты в ясную погоду: B2 около 0,01; дымка — заданные даты.
        return np.full((512, 512), 3000 if when.date() in getattr(self, "hazy", ()) else 100, np.uint16)

    def aux(self, bounds):
        return np.zeros((3, 512, 512), np.float32)

    def model(self, root, bounds, selected):
        self.model_calls.append(bounds)
        return np.full((512, 512), 2, np.uint8)


class LocalBurnBackend(FakeBackend):
    """Гарь кругом вокруг точки огня: пятно замкнуто внутри буфера 3 км."""

    radius_px = 50  # 1 км

    def model(self, root, bounds, selected):
        from pyproj import Transformer

        self.model_calls.append(bounds)
        x, y = Transformer.from_crs(4326, bounds["epsg"], always_xy=True).transform(*FIRE)
        col, row = (x - bounds["x_min"]) / 20, (bounds["y_max"] - y) / 20
        rows, cols = np.mgrid[0:512, 0:512]
        return np.where((rows - row) ** 2 + (cols - col) ** 2 <= self.radius_px ** 2, 2, 0).astype(np.uint8)


# Снимки-заглушки сохраняются во временный кеш, а не в artifacts/ сервиса.
CACHE = Path(tempfile.mkdtemp(prefix="fire-first-tests-"))
atexit.register(shutil.rmtree, CACHE, ignore_errors=True)


def predictor(archive, backend, cache_dir=None):
    # Свой кеш на каждый прогон: результаты на диске иначе переходили бы между тестами.
    cache_dir = cache_dir or Path(tempfile.mkdtemp(dir=CACHE))
    settings = Settings(model_config=PROJECT_ROOT / "configs" / "burn_model.yaml",
                        cdse_client_id="id", cdse_client_secret="secret", firms_map_key="key",
                        satellite_cache_dir=cache_dir / "satellite-cache")
    instance = FireFirstPredictor(settings, backend=backend, archive=archive)
    instance.persistent = PersistentSources([], 500)
    instance.industrial = lambda points: []  # без сети
    instance.weather = None
    return instance


def spec():
    return QuerySpec(AREA, START, END, "bbox")


class FireFirstTests(TestCase):
    def test_no_fire_points_means_no_burns_and_no_model_run(self):
        backend = FakeBackend()
        dataset, prediction = predictor(FakeArchive([]), backend).predict(spec())
        self.assertEqual(prediction["status"], "no_fire_evidence")
        self.assertEqual(dataset.burns, ())
        self.assertEqual(backend.model_calls, [])
        self.assertIn("не зафиксировал", prediction["message"])

    def test_model_runs_only_inside_event_buffers(self):
        backend = FakeBackend()
        points = [point(*FIRE, date(2024, 8, 10)), point(FIRE[0] + 0.003, FIRE[1], date(2024, 8, 11))]
        instance = predictor(FakeArchive(points), backend)
        instance.config["events"]["expand_buffer_m"] = []  # здесь проверяется сам буфер 3 км
        dataset, prediction = instance.predict(spec())
        self.assertEqual(prediction["status"], "ok")
        self.assertEqual(len(prediction["events"]), 1)
        event = prediction["events"][0]
        buffer = shape(event["buffer"]).buffer(0.0005)  # полпикселя 20 м на краях
        self.assertTrue(dataset.burns)
        for burn in dataset.burns:
            self.assertTrue(buffer.contains(burn.geometry))
            self.assertEqual(burn.extra["event_id"], event["event_id"])
            self.assertEqual(burn.extra["fire_evidence"]["n_points"], 2)
        self.assertEqual(event["pair"]["pre"], "2024-07-28")
        self.assertEqual(event["pair"]["post"], "2024-08-25")
        self.assertTrue(backend.model_calls)
        self.assertLess(shape(event["buffer"]).area, AREA.area / 4)
        self.assertEqual(len(dataset.hotspots), 2)

    def test_low_confidence_and_static_points_are_not_evidence(self):
        backend = FakeBackend()
        points = [point(*FIRE, date(2024, 8, 10), confidence="low"),
                  point(*FIRE, date(2024, 8, 11), fire_type=2)]
        _, prediction = predictor(FakeArchive(points), backend).predict(spec())
        self.assertEqual(prediction["status"], "no_fire_evidence")
        self.assertEqual(prediction["evidence"]["excluded"],
                         {"low_confidence": 1, "static_type": 1, "persistent": 0})
        self.assertEqual(backend.model_calls, [])

    def test_persistent_source_creates_no_event(self):
        history = [point(*FIRE, date(2023, month, 3 + month)) for month in range(1, 13)]
        cells = persistent_cells(history, cell_m=400, min_days=5, min_months=3)
        self.assertTrue(cells)
        # Один многодневный пожар в одном месяце постоянным источником не считается.
        single_fire = [point(*FIRE, date(2023, 8, day)) for day in range(1, 10)]
        self.assertEqual(persistent_cells(single_fire, cell_m=400, min_days=5, min_months=3), [])
        # Весенние и осенние палы на одном поле без зимних детекций — не завод.
        seasonal = [point(*FIRE, date(2023, month, day)) for month in (3, 4, 9, 10) for day in (5, 6)]
        self.assertEqual(persistent_cells(seasonal, cell_m=400, min_days=5, min_months=3), [])
        self.assertTrue(persistent_cells(seasonal, cell_m=400, min_days=5, min_months=3, require_winter=False))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mask.geojson"
            path.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[
                    [38, 44], [40, 44], [40, 46], [38, 46], [38, 44]]]}, "properties": {"role": "coverage"}},
                *[{"type": "Feature", "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
                   "properties": {"role": "persistent_source"}} for c in cells]]}), encoding="utf-8")
            backend = FakeBackend()
            instance = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend)
            instance.persistent = PersistentSources([path], 500)
            _, prediction = instance.predict(spec())
        self.assertEqual(prediction["status"], "no_fire_evidence")
        self.assertEqual(prediction["evidence"]["excluded"]["persistent"], 1)
        self.assertTrue(prediction["evidence"]["persistent_mask"]["covers_area"])
        self.assertEqual(backend.model_calls, [])

    def test_cloudy_post_scene_gives_insufficient_data_and_keeps_hotspots(self):
        backend = FakeBackend(cloudy={"2024-08-25": 0.9, "2024-08-30": 0.6})
        dataset, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        self.assertEqual(prediction["status"], "insufficient_data")
        event = prediction["events"][0]
        self.assertEqual((event["status"], event["reason"]), ("insufficient_data", "too_cloudy"))
        self.assertEqual({item["date"] for item in event["rejected_scenes"]}, {"2024-08-25", "2024-08-30"})
        self.assertEqual(backend.model_calls, [])
        self.assertEqual(dataset.burns, ())
        self.assertEqual(len(dataset.hotspots), 1)

    def test_closer_scene_wins_when_no_pair_is_reliable(self):
        # 28.07 → 25.08 и 28.07 → 30.08 — обе пары длиннее 15 дн.: короче лучше, хоть и облачнее.
        backend = FakeBackend(cloudy={"2024-08-25": 0.25, "2024-08-30": 0.02})
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        self.assertEqual(prediction["events"][0]["pair"]["post"], "2024-08-25")

    def test_less_cloudy_candidate_wins_among_reliable_pairs(self):
        backend = FakeBackend(cloudy={"2024-08-12": 0.25, "2024-08-14": 0.02})
        backend.days = [date(2024, 8, 5), date(2024, 8, 12), date(2024, 8, 14), date(2024, 8, 30)]
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        self.assertEqual(prediction["events"][0]["pair"]["post"], "2024-08-14")

    def test_hazy_scene_is_not_used(self):
        # 15.08 по SCL годен (облаков 0 %), но в дымке: тёмные объекты в синем канале яркие.
        backend = FakeBackend()
        backend.days = [date(2024, 8, 5), date(2024, 8, 15), date(2024, 8, 25)]
        backend.hazy = {date(2024, 8, 15)}
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        event = prediction["events"][0]
        self.assertEqual(event["pair"]["post"], "2024-08-25")
        self.assertEqual([r["reason"] for r in event["rejected_scenes"] if r["date"] == "2024-08-15"], ["haze"])

    def test_reliable_pair_beats_clearer_distant_scene(self):
        # Прежнее правило брало безоблачный снимок через 20 дн.; надёжная пара важнее.
        backend = LocalBurnBackend(cloudy={"2024-08-15": 0.25})
        backend.radius_px = 12  # ~18 га на точку, как у настоящих пожаров
        backend.days = [date(2024, 8, 5), date(2024, 8, 15), date(2024, 8, 25)]
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        event = prediction["events"][0]
        self.assertEqual(event["pair"]["post"], "2024-08-15")
        self.assertEqual(event["reliability"], "reliable")

    def test_firms_failure_is_reported_not_silently_empty(self):
        _, prediction = predictor(FakeArchive(error="сеть недоступна"), FakeBackend()).predict(spec())
        self.assertEqual(prediction["status"], "source_unavailable")
        self.assertIn("недоступны", prediction["message"])

    def test_events_cluster_by_distance(self):
        far = (FIRE[0] + 0.1, FIRE[1])
        events = cluster_events([point(*FIRE, date(2024, 8, 10)), point(FIRE[0] + 0.005, FIRE[1], date(2024, 8, 10)),
                                 point(*far, date(2024, 8, 12))], 1000, 3000)
        self.assertEqual(sorted(len(e.points) for e in events), [1, 2])


class FireFirstApiTests(TestCase):
    def test_api_adds_fields_and_keeps_old_ones(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            app = create_app(Settings(db_path=f"{directory}/db.sqlite3", cdse_client_id="id",
                                      cdse_client_secret="secret", firms_map_key="key",
                                      satellite_cache_dir=Path(directory) / "satellite-cache"))
            backend = FakeBackend()
            app.state.fire_first.archive = FakeArchive([point(*FIRE, date(2024, 8, 10))])
            app.state.fire_first.backend = backend
            app.state.fire_first.persistent = PersistentSources([], 500)
            app.state.fire_first.industrial = lambda points: []
            app.state.fire_first.weather = None
            client = clients.enter_context(TestClient(app))
            body = client.post("/api/query", json={"bbox": list(AREA.bounds), "from": START.isoformat(),
                                                   "to": END.isoformat()}).json()
        for key in ("query", "hotspots", "burns", "summary", "data_source", "prediction"):
            self.assertIn(key, body)
        for key in ("model", "scenes", "observed_fraction", "unobserved"):
            self.assertIn(key, body["prediction"])
        self.assertEqual(body["prediction"]["mode"], "fire_first")
        self.assertEqual(body["prediction"]["status"], "ok")
        self.assertEqual(body["summary"]["hotspot_count"], 1)
        feature = body["burns"]["features"][0]["properties"]
        self.assertIn("event_id", feature)
        self.assertEqual(feature["fire_evidence"]["n_points"], 1)


class BlobBackend(FakeBackend):
    """Модель рисует квадраты (смещение по столбцам, полуразмер в пикселях, класс)."""

    def __init__(self, blobs=((0, 20, 2), (110, 5, 3)), **kwargs):
        super().__init__(**kwargs)
        self.blobs = blobs

    def model(self, root, bounds, selected):
        from pyproj import Transformer

        self.model_calls.append(bounds)
        result = np.zeros((512, 512), np.uint8)
        x, y = Transformer.from_crs(4326, bounds["epsg"], always_xy=True).transform(*FIRE)
        col, row = int((x - bounds["x_min"]) / 20), int((bounds["y_max"] - y) / 20)
        for dc, half, value in self.blobs:  # по умолчанию: гарь у огня и пятно в ~2.2 км к востоку
            c = col + dc
            if 0 <= row < 512 and 0 <= c < 512:
                result[max(row - half, 0):row + half, max(c - half, 0):c + half] = value
        return result


class FireFirstLinkingTests(TestCase):
    def test_burn_patch_far_from_fire_is_discarded(self):
        backend = BlobBackend()
        dataset, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        self.assertTrue(dataset.burns)
        self.assertEqual({burn.severity for burn in dataset.burns}, {2})
        self.assertGreater(prediction["events"][0]["discarded_far_from_fire_ha"], 0)

    def test_field_next_to_fire_without_scar_at_detection_is_no_scar(self):
        # Поле 800 м начинается в ~300 м от точки: в круг VIIRS попадает его край, гари там мало.
        backend = BlobBackend(blobs=((35, 20, 2),))
        dataset, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        self.assertEqual(prediction["status"], "no_scar")
        self.assertEqual(prediction["events"][0]["status"], "no_scar")
        self.assertEqual(dataset.burns, ())
        self.assertGreater(prediction["events"][0]["discarded_far_from_fire_ha"], 0)

    def test_weak_night_fire_is_flagged_as_smouldering(self):
        weak = FirePoint(*FIRE, date(2024, 8, 10), "2230", "VIIRS_NOAA20_NRT", "NOAA-20", "nominal", 1.2, None, "N")
        _, prediction = predictor(FakeArchive([weak]), BlobBackend()).predict(spec())
        self.assertTrue(any("тление" in item for item in prediction["events"][0]["limitations"]))

    def test_same_place_weeks_apart_gives_separate_events(self):
        events = cluster_events([point(*FIRE, date(2024, 8, 1)), point(*FIRE, date(2024, 8, 2)),
                                 point(*FIRE, date(2024, 8, 20))], 1000, 3000, link_days=2)
        self.assertEqual(sorted(len(e.points) for e in events), [1, 2])

    def test_overlapping_events_do_not_double_count(self):
        backend = FakeBackend()
        # Два события в 2 км друг от друга (разные дни > link_days): буферы 3 км перекрываются.
        points = [point(*FIRE, date(2024, 8, 10)), point(FIRE[0] + 0.025, FIRE[1], date(2024, 8, 14))]
        dataset, prediction = predictor(FakeArchive(points), backend).predict(spec())
        self.assertEqual(len(prediction["events"]), 2)
        geometries = [burn.geometry for burn in dataset.burns]
        overlap = sum(a.intersection(b).area for i, a in enumerate(geometries) for b in geometries[i + 1:])
        self.assertLess(overlap, 1e-9)


class FireFirstReportingTests(TestCase):
    def test_contours_are_merged_dated_by_fire_and_carry_landcover(self):
        class CroplandBackend(FakeBackend):
            def aux(self, bounds):
                aux = np.zeros((3, 512, 512), np.int16)
                aux[2] = 40  # пашня
                return aux

        dataset, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]),
                                        CroplandBackend()).predict(spec())
        event = prediction["events"][0]
        tiles = {burn.extra["chip_id"] for burn in dataset.burns}
        self.assertLessEqual(len(dataset.burns), 3 * len(tiles))
        self.assertEqual({burn.date for burn in dataset.burns}, {date(2024, 8, 10)})
        self.assertEqual(dataset.burns[0].extra["post_image_date"], "2024-08-25")
        self.assertEqual(set(event["burn_by_landcover_ha"]), {"пашня"})
        self.assertAlmostEqual(event["burn_by_landcover_ha"]["пашня"]["2"], event["burn_area_ha"], delta=1.0)

    def test_long_gap_or_weak_night_fire_is_not_counted_as_reliable(self):
        backend = FakeBackend(cloudy={"2024-08-25": 0.9})  # ближайший ясный post — 30.08, разрыв 33 дня
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        event = prediction["events"][0]
        self.assertEqual(event["reliability"], "unreliable")
        self.assertEqual(prediction["burn_area_reliable_ha"], 0)
        self.assertGreater(prediction["burn_area_unreliable_ha"], 0)
        weak = FirePoint(*FIRE, date(2024, 8, 10), "2230", "VIIRS_NOAA20_NRT", "NOAA-20", "nominal", 1.2, None, "N")
        _, prediction = predictor(FakeArchive([weak]), FakeBackend()).predict(spec())
        self.assertEqual(prediction["events"][0]["reliability"], "unreliable")
        self.assertIn("слабый ночной огонь — вероятно тление", prediction["events"][0]["reliability_reasons"])


class FireFirstCacheTests(TestCase):
    def test_repeat_query_is_served_from_disk_without_network(self):
        shared = Path(tempfile.mkdtemp(dir=CACHE))
        _, first = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), FakeBackend(), shared).predict(spec())
        archive, backend = FakeArchive([point(*FIRE, date(2024, 8, 10))]), FakeBackend()
        dataset, again = predictor(archive, backend, shared).predict(spec())
        self.assertEqual((archive.calls, backend.model_calls), (0, []))
        self.assertEqual(again["events"][0]["burn_area_ha"], first["events"][0]["burn_area_ha"])
        self.assertIn("cached_at", again)
        self.assertTrue(dataset.burns)

    def test_fresh_period_is_provisional_and_expires_in_memory(self):
        from datetime import datetime, timedelta, timezone
        from service.fire_first import _provisional

        old = spec()
        today = datetime.now(timezone.utc).date()
        fresh = QuerySpec(AREA, today - timedelta(days=10), today - timedelta(days=3), "bbox")
        config = {"scenes": {"post_window_days": 45}}
        self.assertFalse(_provisional(old, config, today))
        self.assertTrue(_provisional(fresh, config, today))
        # Окно «после» закрывается через post_window_days после конца периода.
        self.assertFalse(_provisional(fresh, config, fresh.date_to + timedelta(days=46)))

        instance = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), FakeBackend(),
                             Path(tempfile.mkdtemp(dir=CACHE)))
        _, first = instance.predict(old)
        self.assertFalse(first["provisional"])
        # Старый период — окончательный ответ: память не сбрасывается, сколько бы он ни лежал.
        digest = first["result_key"]
        instance._cached_at[digest] -= timedelta(days=3)
        self.assertIs(instance.predict(old)[1], first)

    def test_demo_cases_are_listed(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = clients.enter_context(TestClient(create_app(Settings(
                db_path=f"{directory}/db.sqlite3", satellite_cache_dir=Path(directory) / "cache"))))
            cases = client.get("/api/demo-cases").json()["cases"]
        self.assertTrue(cases)
        self.assertTrue(all({"id", "name", "bbox", "from", "to", "shows"} <= set(case) for case in cases))


class IndustrialContextTests(TestCase):
    def test_osm_objects_near_fire_are_named_not_excluded(self):
        import io
        from service.fire_evidence import IndustrialContext

        payload = json.dumps({"elements": [
            {"tags": {"landuse": "industrial", "name": "Промзона"}, "center": {"lat": FIRE[1] + 0.002, "lon": FIRE[0]}},
            {"tags": {"man_made": "flare"}, "lat": FIRE[1], "lon": FIRE[0] + 0.003},
            # Завод в ~2 км от точки огня не засчитывается.
            {"tags": {"man_made": "works", "name": "Далёкий завод"}, "center": {"lat": FIRE[1] + 0.02, "lon": FIRE[0]}},
        ]}).encode()
        context = IndustrialContext(None, 500, opener=lambda request, timeout: io.BytesIO(payload))
        instance = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), FakeBackend())
        instance.industrial = context
        dataset, prediction = instance.predict(spec())
        event = prediction["events"][0]
        self.assertEqual(event["status"], "ok")
        self.assertTrue(dataset.burns)
        self.assertEqual([o["kind"] for o in event["industrial_context"]], ["промзона", "факел"])
        self.assertTrue(any("техногенным" in item for item in event["limitations"]))


class FailedEventsAreNotCachedTests(TestCase):
    def test_event_with_network_failure_is_recomputed_next_time(self):
        class FlakyBackend(FakeBackend):
            def scene(self, bounds, when):
                raise OSError("The read operation timed out")

        shared = Path(tempfile.mkdtemp(dir=CACHE))
        _, first = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), FlakyBackend(), shared).predict(spec())
        self.assertEqual(first["events"][0]["status"], "source_unavailable")
        archive = FakeArchive([point(*FIRE, date(2024, 8, 10))])
        _, second = predictor(archive, FakeBackend(), shared).predict(spec())
        self.assertEqual(archive.calls, 1)
        self.assertEqual(second["events"][0]["status"], "ok")


class LookAtResultTests(TestCase):
    """Выгрузки, по которым результат можно проверить глазами: KML и снимки события."""

    def _client(self, directory, clients):
        app = create_app(Settings(db_path=f"{directory}/db.sqlite3", cdse_client_id="id",
                                  cdse_client_secret="secret", firms_map_key="key",
                                  satellite_cache_dir=Path(directory) / "satellite-cache"))
        app.state.fire_first.archive = FakeArchive([point(*FIRE, date(2024, 8, 10))])
        app.state.fire_first.backend = FakeBackend()
        app.state.fire_first.persistent = PersistentSources([], 500)
        app.state.fire_first.industrial = lambda points: []
        app.state.fire_first.weather = None
        return clients.enter_context(TestClient(app))

    def test_kml_has_burns_and_fire_points(self):
        import xml.etree.ElementTree as ET

        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = self._client(directory, clients)
            params = {"bbox": ",".join(map(str, AREA.bounds)), "from": START.isoformat(), "to": END.isoformat()}
            response = client.get("/api/export/fires.kml", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertIn("kml", response.headers["content-type"])
        root = ET.fromstring(response.content)
        ns = {"k": "http://www.opengis.net/kml/2.2"}
        self.assertTrue(root.findall(".//k:Placemark/k:Polygon", ns))
        self.assertEqual(len(root.findall(".//k:Placemark/k:Point", ns)), 1)

    def test_event_preview_is_png_from_saved_scenes(self):
        with TemporaryDirectory() as directory, ExitStack() as clients:
            client = self._client(directory, clients)
            body = {"bbox": list(AREA.bounds), "from": START.isoformat(), "to": END.isoformat()}
            event_id = client.post("/api/query", json=body).json()["prediction"]["events"][0]["event_id"]
            image = client.post(f"/api/events/{event_id}/preview.png", json=body)
            missing = client.post("/api/events/fire-unknown/preview.png", json=body)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertTrue(image.content.startswith(b"\x89PNG"))
        self.assertEqual(missing.status_code, 404)


class BrightCloudTests(TestCase):
    def test_bright_pixels_missed_by_scl_become_cloud(self):
        from service.fire_first import _mark_bright_clouds

        raster = np.full((10, 4, 4), 800, np.uint16)
        raster[9] = 5  # SCL: голая почва
        raster[0, 0, 0] = 2500  # ярко в синем — облако, пропущенное SCL
        raster[0, 1, 1], raster[9, 1, 1] = 2500, 6  # вода не трогается
        marked = _mark_bright_clouds(raster, 0.15)
        self.assertEqual(marked[9, 0, 0], 8)
        self.assertEqual(marked[9, 1, 1], 6)
        self.assertEqual(int((marked[9] == 8).sum()), 1)
        self.assertEqual(raster[9, 0, 0], 5)  # исходный массив не меняется


class IndustrialShareTests(TestCase):
    def test_object_near_few_points_is_not_called_technogenic(self):
        import io
        from service.fire_evidence import IndustrialContext

        payload = json.dumps({"elements": [
            {"tags": {"landuse": "industrial"}, "center": {"lat": FIRE[1] + 0.002, "lon": FIRE[0]}}]}).encode()
        context = IndustrialContext(None, 500, opener=lambda request, timeout: io.BytesIO(payload))
        points = [point(*FIRE, date(2024, 8, 10))] + [point(FIRE[0] + 0.004 * i, FIRE[1] - 0.006, date(2024, 8, 10))
                                                      for i in range(3)]
        instance = predictor(FakeArchive(points), FakeBackend())
        instance.industrial = context
        _, prediction = instance.predict(spec())
        notes = [item for event in prediction["events"] for item in event["limitations"] if "OpenStreetMap" in item]
        self.assertTrue(notes)
        self.assertTrue(all("техногенным" not in note for note in notes))
        self.assertTrue(any("к 1 из" in note for note in notes))


class BareBeforeFireBackend(FakeBackend):
    """Снимок «до» 28.07 — поле зелёное; 05.08 (облачно на 50 %) чётные столбцы уже голые; после — всё голое."""

    def __init__(self):
        super().__init__(cloudy={"2024-08-05": 0.5})
        self.days = [date(2024, 7, 20), date(2024, 7, 28), date(2024, 8, 5), date(2024, 8, 25), date(2024, 8, 30)]

    def scene(self, bounds, when):
        data = super().scene(bounds, when)
        data[6], data[8] = 3000, 1000  # NBR 0.5
        if when.date() == date(2024, 8, 5):
            data[8][:, ::2] = 3000  # NBR 0: убрано или вспахано до огня
        if when.date() >= date(2024, 8, 25):
            data[8] = 3000
        return data


class BareBeforeFireTests(TestCase):
    def run_case(self, scenes):
        instance = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), BareBeforeFireBackend())
        instance.config["scenes"]["timing_check_scenes"] = scenes
        _, prediction = instance.predict(spec())
        return prediction["events"][0]

    def test_field_bare_before_fire_is_not_burn(self):
        checked, unchecked = self.run_case(3), self.run_case(0)
        self.assertEqual(checked["pair"]["pre"], "2024-07-28")  # облачный 05.08 парой не стал
        self.assertEqual(checked["pre_fire_check"]["scenes"], ["2024-08-05"])
        self.assertGreater(checked["pre_fire_check"]["removed_ha"], 0)
        self.assertLess(checked["burn_area_ha"], unchecked["burn_area_ha"])
        self.assertGreater(checked["burn_area_ha"], 0)  # нечётные столбцы потемнели от огня
        self.assertTrue(any("потемнели ещё до огня" in note for note in checked["limitations"]))
        self.assertEqual(unchecked["pre_fire_check"]["removed_ha"], 0)

    def test_unchecked_gap_is_reported(self):
        backend = FakeBackend()  # между 28.07 и огнём 10.08 снимков нет
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        event = prediction["events"][0]
        self.assertEqual(event["pre_fire_check"]["scenes"], [])
        self.assertTrue(any("не проверено, не убрали ли поле" in note for note in event["limitations"]))


class EventWeatherTests(TestCase):
    def test_weather_at_first_detection(self):
        from service.weather import WeatherError

        class FakeWeather:
            calls = []

            def at(self, lon, lat, when):
                self.calls.append(when)
                if len(self.calls) > 1:
                    raise WeatherError("Open-Meteo недоступен")
                return {"temperature_c": 30.0, "danger_class": 4}

        points = [point(*FIRE, date(2024, 8, 11)), point(FIRE[0] + 0.003, FIRE[1], date(2024, 8, 10))]
        instance = predictor(FakeArchive(points), FakeBackend())
        instance.weather = FakeWeather()
        _, prediction = instance.predict(spec())
        event = prediction["events"][0]
        self.assertEqual(event["weather"]["danger_class"], 4)
        self.assertEqual(FakeWeather.calls[0], datetime(2024, 8, 10, 10, 30, tzinfo=timezone.utc))


class LaterDarkeningBackend(FakeBackend):
    """Огонь 10.08; 13.08 (облачно на 50 %) чётные столбцы ещё зелёные; к 25.08 голое всё."""

    def __init__(self):
        super().__init__(cloudy={"2024-08-13": 0.5})
        self.days = [date(2024, 7, 20), date(2024, 7, 28), date(2024, 8, 13), date(2024, 8, 25), date(2024, 8, 30)]

    def scene(self, bounds, when):
        data = super().scene(bounds, when)
        data[6], data[8] = 3000, 1000  # NBR 0.5
        if when.date() == date(2024, 8, 13):
            data[8][:, 1::2] = 3000  # нечётные столбцы уже гарь, чётные ещё зелёные
        if when.date() >= date(2024, 8, 25):
            data[8] = 3000
        return data


class LaterDarkeningTests(TestCase):
    def test_field_darkened_after_fire_is_not_burn(self):
        def run(scenes):
            instance = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), LaterDarkeningBackend())
            instance.config["scenes"]["timing_check_scenes"] = scenes
            return instance.predict(spec())[1]["events"][0]

        checked, unchecked = run(3), run(0)
        self.assertEqual(checked["pair"]["post"], "2024-08-25")
        self.assertEqual(checked["post_fire_check"]["scenes"], ["2024-08-13"])
        self.assertGreater(checked["post_fire_check"]["removed_ha"], 0)
        self.assertLess(checked["burn_area_ha"], unchecked["burn_area_ha"])
        self.assertGreater(checked["burn_area_ha"], 0)
        self.assertTrue(any("потемнели позже" in note for note in checked["limitations"]))


class HarvestBackend(LocalBurnBackend):
    """После огня: в левой половине все видимые каналы посветлели (убрано), в правой — только красный
    (сгоревшая зелень: хлорофилл ушёл, уголь затемнил синий и зелёный)."""

    cover = 40  # пашня (ESA WorldCover)
    straw_offset_px = 25  # солома — край пятна (~20 %), уголь — у точки

    def aux(self, bounds):
        data = np.zeros((3, 512, 512), np.float32)
        data[2] = self.cover
        return data

    def scene(self, bounds, when):
        from pyproj import Transformer

        data = super().scene(bounds, when)
        data[0], data[1], data[2] = 300, 600, 300  # зелёная растительность
        if when.date() >= date(2024, 8, 25):
            # Граница половин проходит через точку огня: пятно делится пополам.
            x, _ = Transformer.from_crs(4326, bounds["epsg"], always_xy=True).transform(*FIRE)
            split = int((x - bounds["x_min"]) / 20) - self.straw_offset_px
            data[0][:, :split], data[1][:, :split], data[2][:, :split] = 900, 1100, 1300  # солома: ярче везде
            data[0][:, split:], data[1][:, split:], data[2][:, split:] = 280, 400, 450  # уголь: красный выше
        return data


class HarvestCheckTests(TestCase):
    def test_forest_is_not_checked(self):
        # В лесу огонь в кронах оставляет бурую листву — светлее во всех каналах, но уборки там нет.
        class ForestFire(HarvestBackend):
            cover = 10

        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), ForestFire()).predict(spec())
        self.assertEqual(prediction["events"][0]["harvest_check"]["removed_ha"], 0)

    def test_brightened_in_all_visible_bands_is_harvest_not_burn(self):
        instance = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), HarvestBackend())
        _, prediction = instance.predict(spec())
        event = prediction["events"][0]
        check = event["harvest_check"]
        self.assertTrue(check["performed"])
        self.assertTrue(check["passed"])  # уборкой ~20 % изменений — меньше порога 0,35
        self.assertTrue(check["near_points"]["confirmed"])  # у самой точки — уголь
        self.assertGreater(check["removed_ha"], 0)
        self.assertGreater(event["burn_area_ha"], 0)  # правая половина — гарь на зелёном покрове осталась
        self.assertLess(check["removed_share"], 0.9)
        self.assertTrue(any("а не выжжено" in note for note in event["limitations"]))

    def test_red_only_brightening_of_green_cover_is_kept(self):
        class GreenBurn(HarvestBackend):
            def scene(self, bounds, when):
                data = super().scene(bounds, when)
                if when.date() >= date(2024, 8, 25):
                    data[0], data[1], data[2] = 280, 400, 450
                return data

        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), GreenBurn()).predict(spec())
        self.assertEqual(prediction["events"][0]["harvest_check"]["removed_ha"], 0)


class SuspicionSignalTests(TestCase):
    def test_contour_against_buffer_edge_is_unreliable(self):
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), FakeBackend()).predict(spec())
        event = prediction["events"][0]  # гарь по всей плитке: упирается в буфер со всех сторон
        self.assertTrue(event["buffer_edge"]["touches"])
        self.assertEqual(event["reliability"], "unreliable")
        # Зона расширялась, но гарь продолжается и за ней (подмена рисует её по всей плитке).
        self.assertFalse(event["buffer_expansion"]["closed"])
        self.assertTrue(any("продолжается и за расширенной зоной" in r for r in event["reliability_reasons"]))

    def test_closed_scar_is_reliable_and_area_per_point_is_reported(self):
        backend = LocalBurnBackend()
        backend.radius_px = 16
        backend.days = [date(2024, 8, 5), date(2024, 8, 15)]
        points = [point(*FIRE, date(2024, 8, 10)), point(FIRE[0] + 0.002, FIRE[1], date(2024, 8, 10))]
        _, prediction = predictor(FakeArchive(points), backend).predict(spec())
        event = prediction["events"][0]
        self.assertFalse(event["buffer_edge"]["touches"])
        self.assertEqual(event["reliability"], "reliable")
        self.assertAlmostEqual(event["area_per_point_ha"], event["burn_area_ha"] / 2, places=1)


class MostlyHarvestTests(TestCase):
    def test_event_where_most_change_is_harvest_is_unreliable(self):
        class MostlyStraw(HarvestBackend):
            def scene(self, bounds, when):
                from pyproj import Transformer

                data = super().scene(bounds, when)
                if when.date() >= date(2024, 8, 25):
                    # Уголь — квадрат 60×60 пикселей вокруг точки огня, остальное пятно — солома (~54 %).
                    x, y = Transformer.from_crs(4326, bounds["epsg"], always_xy=True).transform(*FIRE)
                    col, row = int((x - bounds["x_min"]) / 20), int((bounds["y_max"] - y) / 20)
                    data[0], data[1], data[2] = 900, 1100, 1300
                    near = (slice(row - 30, row + 30), slice(col - 30, col + 30))
                    data[0][near], data[1][near], data[2][near] = 280, 400, 450
                return data

        backend = MostlyStraw()
        backend.days = [date(2024, 8, 5), date(2024, 8, 25)]
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        event = prediction["events"][0]
        self.assertFalse(event["harvest_check"]["passed"])
        self.assertEqual(event["reliability"], "unreliable")
        self.assertTrue(any("уборка (поля посветлели)" in r for r in event["reliability_reasons"]))


class HarvestPreviewTests(TestCase):
    def test_preview_draws_harvest_layer(self):
        from service.event_preview import render_event

        instance = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), HarvestBackend())
        dataset, prediction = instance.predict(spec())
        event = prediction["events"][0]
        root = instance.settings.satellite_cache_dir / "annotation-scenes" / prediction["result_key"] / "filtered"
        self.assertTrue(any(root.glob("*_harvest.tif")))
        image = render_event(dataset, prediction, event["event_id"], instance.settings.satellite_cache_dir,
                             instance.config["scenes"]["cloud_classes"])
        self.assertTrue(image.startswith(b"\x89PNG"))


class PersistentMaskPathTests(TestCase):
    def test_local_directories_do_not_reach_the_api(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mask.geojson"
            path.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
            sources = PersistentSources([path], 500)
        self.assertEqual(sources.loaded, ["mask.geojson"])  # вне проекта — только имя файла
        self.assertEqual(sources.paths, [path])


class ReliabilitySignalTests(TestCase):
    def test_area_per_point_above_clms_maximum_is_unreliable(self):
        # Круг 1 км вокруг одной точки — 314 га на точку при максимуме у пожаров CLMS 40 га.
        backend = LocalBurnBackend()
        backend.days = [date(2024, 8, 5), date(2024, 8, 15)]
        _, prediction = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend).predict(spec())
        event = prediction["events"][0]
        self.assertGreater(event["area_per_point_ha"], 40)
        self.assertEqual(event["reliability"], "unreliable")
        self.assertTrue(any("на одну термоточку" in r for r in event["reliability_reasons"]))

    def run_season(self, months):
        backend = HarvestBackend()
        backend.straw_offset_px = -5  # солома захватывает и круг вокруг самой точки
        backend.days = [date(2024, 8, 5), date(2024, 8, 25)]
        instance = predictor(FakeArchive([point(*FIRE, date(2024, 8, 10))]), backend)
        instance.config["events"]["harvest_season_months"] = months
        return instance.predict(spec())[1]["events"][0]

    def test_harvest_season_needs_burn_confirmed_at_the_points(self):
        event = self.run_season([8])
        self.assertFalse(event["harvest_check"]["near_points"]["confirmed"])
        self.assertEqual(event["reliability"], "unreliable")
        self.assertTrue(any(r.startswith("требует подтверждения") for r in event["reliability_reasons"]))
        outside = self.run_season([9, 10])
        self.assertFalse(any(r.startswith("требует подтверждения") for r in outside["reliability_reasons"]))


class BufferExpansionTests(TestCase):
    def test_scar_closing_within_wider_zone_is_not_penalised(self):
        # Пятно радиусом 3,5 км вокруг точки: упирается в буфер 3 км, но замыкается в 6 км.
        backend = LocalBurnBackend()
        backend.radius_px = 175
        backend.days = [date(2024, 8, 5), date(2024, 8, 15)]
        points = [point(*FIRE, date(2024, 8, 10)) for _ in range(120)]  # ~32 га на точку
        _, prediction = predictor(FakeArchive(points), backend).predict(spec())
        event = prediction["events"][0]
        expansion = event["buffer_expansion"]
        self.assertTrue(expansion["closed"])
        self.assertEqual(expansion["final_buffer_m"], 6000)
        self.assertFalse(event["buffer_edge"]["touches"])
        self.assertFalse(any("контур" in r for r in event["reliability_reasons"]))
        self.assertGreater(event["burn_area_ha"], 3000)  # пятно целиком, а не обрезанное буфером 3 км
