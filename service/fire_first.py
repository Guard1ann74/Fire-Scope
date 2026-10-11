"""Режим «огонь прежде гари»: модель гари только вокруг подтверждённых очагов.

Порядок для одного запроса:
1. термоточки FIRMS за период → фильтры → события (service.fire_evidence);
2. для каждого события — пара Sentinel-2 до/после, пригодная над буфером события;
3. модель гари в плитках буфера, результат обрезается буфером и областью запроса.

Внешние вызовы собраны в CopernicusBackend, чтобы тесты подменяли их без сети.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform, unary_union

from service.config import PROJECT_ROOT, Settings
from service.fire_evidence import (FireEvent, FireSourceError, FirmsArchive, IndustrialContext,
                                   PersistentSources, cluster_events, load_service_config, select_evidence)
from service.query import QueryError, QuerySpec
from service.sources import Burn, DataSourceError, Dataset, Hotspot
from service.weather import Weather, WeatherError, observed_at

# Версия логики ответа: входит в ключ кеша на диске, поэтому после изменения
# правил старые результаты не выдаются. Повышать при любом изменении расчёта.
RESULT_VERSION = "2026-10-12.3"

VIIRS_LIMITATION = ("Разрешение VIIRS 375 м: термоточки подтверждают факт огня, но не задают "
                    "границу гари.")


class CopernicusBackend:
    """Каталог, снимки, вспомогательные слои и модель — реальные вызовы."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token: tuple[float, str] | None = None

    def token(self) -> str:
        import time

        from src.collector.burn_severity import _token

        if self._token is None or time.monotonic() - self._token[0] > 1800:
            self._token = (time.monotonic(),
                           _token(self.settings.cdse_client_id, self.settings.cdse_client_secret))
        return self._token[1]

    def catalogue(self, footprint, start: date, end: date) -> list[dict]:
        from src.collector.copernicus import search_scenes

        return search_scenes(mapping(footprint), start, end, "sentinel-2-l2a",
                             limit=100, max_pages=5)["items"]

    def _fetch(self, bounds: dict, when: datetime, bands: tuple[str, ...]) -> np.ndarray:
        """Запрос Process API с повторами: обрыв или 5xx у Copernicus — временные, а один сбой
        иначе превращал всё событие в «источник недоступен». 4xx не повторяется."""
        import time
        from urllib.error import HTTPError

        from src.collector.burn_severity import _fetch_raster

        for attempt in range(3):
            try:
                return _fetch_raster(self.token(), bounds, when, bands)
            except HTTPError as error:
                if error.code < 500 or attempt == 2:
                    raise
            except OSError:
                if attempt == 2:
                    raise
            time.sleep(5 * (attempt + 1))
        raise RuntimeError("unreachable")

    def scl(self, bounds: dict, when: datetime) -> np.ndarray:
        return self._fetch(bounds, when, ("SCL",))[0]

    def blue(self, bounds: dict, when: datetime) -> np.ndarray:
        """Синий канал B02 (отражение ×10000) — для проверки дымки у кандидатов пары."""
        return self._fetch(bounds, when, ("B02",))[0]

    def scene(self, bounds: dict, when: datetime) -> np.ndarray:
        """Каналы Sentinel-2 и SCL без проверки «мало данных» сборщика.

        Сборщик бракует плитку, если в SWIR много нулей, — а у моря они и есть.
        Пропуски над очагом здесь уже проверены по SCL при подборе пары.
        """
        from src.collector.burn_severity import OPTICAL_BANDS

        optical = self._fetch(bounds, when, OPTICAL_BANDS)
        scl = self._fetch(bounds, when, ("SCL",))
        if np.max(scl) > 11:
            raise ValueError("Copernicus returned invalid SCL classes")
        return np.concatenate([optical, scl], axis=0)

    def aux(self, bounds: dict) -> np.ndarray:
        from src.collector.enrichment import _aux

        return _aux(bounds, self.settings.satellite_cache_dir)[0]

    def model(self, root: Path, bounds: dict, selected: dict) -> np.ndarray:
        from src.collector.burn_severity import PIXELS
        from src.dataset.chips import Chip
        from src.pipeline import predict

        chip = Chip(bounds["chip_id"], "burn_severity", PIXELS, PIXELS, root,
                    {key: str(value) for key, value in bounds.items()})
        return predict(chip, selected)


@dataclass
class Candidate:
    item: dict
    when: datetime
    cloud: float
    nodata: float
    scl: list[np.ndarray]
    haze: float | None = None  # 5-й процентиль B02 ясных пикселей; считается только у лучшей пары


class FireFirstPredictor:
    def __init__(self, settings: Settings, backend: CopernicusBackend | None = None,
                 archive: FirmsArchive | None = None) -> None:
        self.settings = settings
        self.config = load_service_config(settings.service_config)
        evidence = self.config["evidence"]
        self.archive = archive or FirmsArchive(
            settings.firms_map_key, settings.satellite_cache_dir.parent / "firms-cache",
            evidence["platforms"], evidence["window_days"])
        persistent = self.config["persistent_sources"]
        # Пути в конфиге — от корня проекта, как и остальные пути сервиса.
        self.persistent = PersistentSources([PROJECT_ROOT / path for path in persistent["files"]],
                                            persistent["radius_m"])
        self.backend = backend or CopernicusBackend(settings)
        self.industrial = IndustrialContext(settings.satellite_cache_dir.parent / "osm-cache",
                                            self.config["events"].get("industrial_radius_m", 500))
        self.weather: Weather | None = Weather(settings.satellite_cache_dir.parent / "weather-cache")
        self._cache: OrderedDict[str, tuple[Dataset, dict[str, Any]]] = OrderedDict()
        self._lock = Lock()

    # ------------------------------------------------------------ запрос

    def predict(self, spec: QuerySpec) -> tuple[Dataset, dict[str, Any]]:
        from src.collector.burn_severity import GSD, PIXELS
        from src.config import load_config

        if spec.date_to >= datetime.now(timezone.utc).date():
            raise QueryError("Нужен снимок после события: выберите дату окончания раньше сегодняшней")
        limit = float(self.config["events"]["max_area_side_km"]) * 1000
        west, south, east, north = spec.area.bounds
        epsg = utm_epsg_for(spec.area)
        forward = Transformer.from_crs(4326, epsg, always_xy=True).transform
        minx, miny, maxx, maxy = transform(forward, box(west, south, east, north)).bounds
        if maxx - minx > limit or maxy - miny > limit:
            raise QueryError(f"Область {(maxx - minx) / 1000:.0f} × {(maxy - miny) / 1000:.0f} км больше "
                             f"допустимой: до {limit / 1000:.0f} км по каждой стороне")
        if not self.settings.model_config.is_file():
            raise DataSourceError(f"Конфигурация модели не найдена: {self.settings.model_config}")
        masks = "".join(str(path.stat().st_mtime_ns) for path in self.persistent.paths)
        digest = hashlib.sha256(
            RESULT_VERSION.encode() + spec.area.wkb + spec.date_from.isoformat().encode() + spec.date_to.isoformat().encode()
            + self.settings.model_config.read_bytes()  # содержимое правила, а не время правки файла
            # Содержимое параметров, а не время правки файла; раздел мониторинга на гарь не влияет.
            + json.dumps({k: v for k, v in self.config.items() if k != "monitor"}, sort_keys=True).encode()
            + masks.encode()
        ).hexdigest()
        with self._lock:
            if digest in self._cache:
                self._cache.move_to_end(digest)
                return self._cache[digest]
            result = self._load(digest)
            if result is None:
                selected = load_config(self.settings.model_config)["burn_severity"]
                result = self._compute(spec, selected, digest, GSD, PIXELS)
                # Сбой сети по любому событию не закрепляем: следующий запрос попробует снова.
                failed = result[1]["status"] == "source_unavailable" or any(
                    event["status"] == "source_unavailable" for event in result[1].get("events", []))
                if not failed:
                    self._store(digest, result, spec)
            # По ключу ответа находятся сохранённые снимки событий (картинки «до/после»).
            result[1]["result_key"] = digest
            self._cache[digest] = result
            if len(self._cache) > 8:
                self._cache.popitem(last=False)
            return result

    def _compute(self, spec: QuerySpec, selected: dict, digest: str, gsd: int,
                 pixels: int) -> tuple[Dataset, dict[str, Any]]:
        stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        model_name = selected["model"]
        base = {"mode": "fire_first", "provider": "NASA FIRMS + Copernicus Data Space",
                "model": model_name, "generated_at": stamp, "events": [], "scenes": [],
                "observed_fraction": None, "unobserved": None}
        try:
            points, sources, failures = self.archive.points(spec.area.bounds, spec.date_from, spec.date_to)
        except FireSourceError as error:
            return (Dataset((), (), "NASA FIRMS", (), stamp),
                    {**base, "status": "source_unavailable",
                     "message": f"Термоточки NASA FIRMS недоступны: {error}. Без доказательства огня "
                                "гарь не оценивается.",
                     "evidence": None})
        points = [p for p in points if spec.area.intersects(box(p.lon, p.lat, p.lon, p.lat))]
        evidence = select_evidence(points, self.config, self.persistent, spec.area.bounds, sources, failures)
        events_cfg = self.config["events"]
        events = cluster_events(evidence.kept, events_cfg["link_radius_m"], events_cfg["buffer_m"],
                                events_cfg.get("link_days"))
        evidence_info = {
            "sources": evidence.sources, "points_total": evidence.points_total,
            "points_kept": len(evidence.kept), "excluded": evidence.excluded,
            "failures": evidence.failures[:5],
            "persistent_mask": {"files": self.persistent.loaded, "covers_area": evidence.mask_covers_area},
        }
        hotspots = tuple(_hotspot(point, event.id) for event in events for point in event.points)
        if not events:
            return (Dataset(hotspots, (), "NASA FIRMS", tuple(sources), stamp),
                    {**base, "status": "no_fire_evidence", "evidence": evidence_info,
                     "message": "За выбранный период NASA FIRMS не зафиксировал в области подтверждённых "
                                "очагов огня. Гарь не оценивалась: падение растительного индекса без "
                                "огня (уборка, вспашка, засуха) гарью не считается."})

        burns: list[Burn] = []
        reports = []
        scenes = []
        unobserved = []
        seen_total = area_total = 0.0
        claimed = None  # гарь уже отнесённых событий: буферы соседних событий перекрываются
        for index, event in enumerate(events):
            if index >= events_cfg["max_events"]:
                reports.append(_event_report(event, "not_processed", "event_limit",
                                             f"Обработано {events_cfg['max_events']} крупнейших событий; "
                                             "уменьшите область или период, чтобы оценить остальные.",
                                             evidence))
                continue
            report, event_burns, event_scenes, hidden, seen, total = self._event(
                event, spec, selected, digest, evidence, gsd, pixels)
            self._industrial(report, event.points)
            self._weather(report, event.points)
            if claimed is not None and event_burns:
                event_burns = _without(event_burns, claimed, event.epsg)
                forward = Transformer.from_crs(4326, event.epsg, always_xy=True).transform
                report["burn_area_ha"] = round(
                    sum(transform(forward, burn.geometry).area for burn in event_burns) / 1e4, 2)
                report["burn_by_landcover_ha"] = _landcover_of(event_burns)
            if event_burns:
                current = unary_union([burn.geometry for burn in event_burns])
                claimed = current if claimed is None else unary_union([claimed, current])
            reports.append(report)
            burns.extend(event_burns)
            scenes.extend(event_scenes)
            unobserved.extend(hidden)
            seen_total += seen
            area_total += total
        statuses = {report["status"] for report in reports}
        status = ("ok" if "ok" in statuses else "no_scar" if "no_scar" in statuses
                  else "insufficient_data")
        processed = sum(report["status"] == "ok" for report in reports)
        without_scar = sum(report["status"] == "no_scar" for report in reports)
        hidden_area = unary_union(unobserved).intersection(spec.area) if unobserved else None
        message = (f"Событий огня: {len(events)}; гарь на снимках подтверждена для {processed}"
                   + (f", у {without_scar} огонь есть, но гари в месте детекций не видно" if without_scar else "")
                   + ". Модель гари запускалась только вокруг подтверждённых очагов.")
        if status == "no_scar":
            message = (f"Событий огня: {len(events)}. Огонь зафиксирован, но на снимках Sentinel-2 в местах "
                       "детекций гари не видно: возможно тление, мелкий огонь или гарь меньше пикселя VIIRS. "
                       "Изменения на соседних полях гарью не считаются.")
        elif status != "ok":
            message = (f"Событий огня: {len(events)}, но ни для одного нет пригодной пары снимков "
                       "Sentinel-2: гарь не оценивалась. Термоточки показаны на карте.")
        reliable_ha = sum(r["burn_area_ha"] or 0 for r in reports if r.get("reliability") == "reliable")
        unreliable_ha = sum(r["burn_area_ha"] or 0 for r in reports if r.get("reliability") == "unreliable")
        by_landcover = {"reliable": _sum_landcover(r for r in reports if r.get("reliability") == "reliable"),
                        "all": _sum_landcover(reports)}
        return (Dataset(hotspots, tuple(burns), "NASA FIRMS + Copernicus Data Space", tuple(sources), stamp),
                {**base, "status": status, "message": message, "evidence": evidence_info,
                 "events": reports, "scenes": scenes,
                 # Площадь надёжных событий (короткий разрыв между снимками, не слабый ночной огонь)
                 # и остальных; по типам земель и степеням — основа оценки ущерба.
                 "burn_area_reliable_ha": round(reliable_ha, 2),
                 "burn_area_unreliable_ha": round(unreliable_ha, 2),
                 "burn_by_landcover_ha": by_landcover,
                 "observed_fraction": round(seen_total / area_total, 4) if area_total else None,
                 "unobserved": mapping(hidden_area) if hidden_area is not None and not hidden_area.is_empty
                 else None})

    def _industrial(self, report: dict[str, Any], points) -> None:
        """Пометка о промышленных объектах OpenStreetMap рядом с точками огня (не фильтр)."""
        objects = self.industrial(points)
        report["industrial_context"] = objects
        report["limitations"] = [item for item in report["limitations"] if not item.startswith(_OSM_NOTES)]
        if objects is None:
            report["limitations"].append("Проверка промышленных объектов OpenStreetMap не удалась "
                                         "(Overpass недоступен).")
        elif objects:
            listed = ", ".join(f"{o['kind']}{' «' + o['name'] + '»' if o['name'] else ''}" for o in objects[:4])
            near, total = objects[0].get("points_near_any", 0), objects[0].get("points_total", len(points))
            verdict = ("Огонь может быть техногенным." if near * 2 >= total
                       else "Остальные точки огня от них дальше — на происхождение пожара это не указывает.")
            report["limitations"].append(
                f"OpenStreetMap: ближе {self.config['events'].get('industrial_radius_m', 500):.0f} м к {near} из {total} точек огня — "
                f"{listed}. {verdict}")

    def _weather(self, report: dict[str, Any], points) -> None:
        """Погода в центре события на время первой детекции огня (Open-Meteo)."""
        if self.weather is None:
            return
        first = min(points, key=observed_at)
        lon, lat = report["centre"]
        try:
            report["weather"] = self.weather.at(lon, lat, observed_at(first))
        except WeatherError as error:
            report["weather"] = {"error": str(error)}

    def _refresh_context(self, dataset: Dataset, prediction: dict[str, Any]) -> None:
        """Повторить проверку OSM и погоду в сохранённом ответе: источник мог быть недоступен при расчёте."""
        from types import SimpleNamespace

        for report in prediction.get("events", []):
            if report["status"] == "not_processed":
                continue
            points = [SimpleNamespace(lon=h.geometry.x, lat=h.geometry.y, date=h.date,
                                      time_utc=h.extra.get("time_utc"))
                      for h in dataset.hotspots if h.extra.get("event_id") == report["event_id"]]
            if not points:
                continue
            self._industrial(report, points)
            if not report.get("weather") or "error" in report["weather"]:
                self._weather(report, points)

    # --------------------------------------------- кеш результатов на диске

    def _result_path(self, digest: str) -> Path:
        return self.settings.satellite_cache_dir / "fire-first-results" / f"{digest}.json"

    def _store(self, digest: str, result: tuple[Dataset, dict[str, Any]], spec: QuerySpec) -> None:
        """Сохранить ответ: повтор запроса не зависит от сети и не тратит квоту.

        Если после периода ещё могут появиться снимки (окно поиска «после» не
        закончилось), запись временная и через сутки пересчитывается.
        """
        import json

        dataset, prediction = result
        window_end = spec.date_to + timedelta(days=self.config["scenes"]["post_window_days"])
        record = {"final": window_end < datetime.now(timezone.utc).date(),
                  "stored_at": datetime.now(timezone.utc).isoformat(),
                  "dataset": _dump_dataset(dataset), "prediction": prediction}
        path = self._result_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _load(self, digest: str) -> tuple[Dataset, dict[str, Any]] | None:
        import json

        path = self._result_path(digest)
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(record["stored_at"])
        if not record["final"] and age > timedelta(days=1):
            return None
        prediction = {**record["prediction"], "cached_at": record["stored_at"]}
        dataset = _load_dataset(record["dataset"])
        self._refresh_context(dataset, prediction)
        return dataset, prediction

    # ------------------------------------------------------ одно событие

    def _event(self, event: FireEvent, spec: QuerySpec, selected: dict, digest: str, evidence,
               gsd: int, pixels: int):
        from rasterio.features import shapes
        from rasterio.transform import from_origin
        from src.collector.burn_severity import _acquired, _write_raster, plan_chips

        events_cfg = self.config["events"]
        footprint = event.buffer.intersection(spec.area)
        _, tiles = plan_chips(event.buffer.bounds, anchor="area")
        if len(tiles) > events_cfg["max_tiles_per_event"]:
            return (_event_report(event, "insufficient_data", "event_too_large",
                                  f"Буфер события занимает {len(tiles)} плиток 10×10 км при пределе "
                                  f"{events_cfg['max_tiles_per_event']}.", evidence), [], [], [], 0, 0)
        masks = [_mask(footprint, bounds, pixels, gsd) for bounds in tiles]
        try:
            items = self.backend.catalogue(
                event.buffer, event.first_detected - timedelta(days=self.config["scenes"]["pre_window_days"]),
                min(event.last_detected + timedelta(days=self.config["scenes"]["post_window_days"]),
                    datetime.now(timezone.utc).date()))
            pre, post, rejected, reason = self._pair(event, items, tiles, masks, _acquired)
        except (OSError, RuntimeError, ValueError) as error:
            return (_event_report(event, "source_unavailable", "copernicus_unavailable",
                                  f"Copernicus недоступен: {error}", evidence), [], [], [], 0, 0)
        if reason:
            report = _event_report(event, "insufficient_data", reason, _REASONS[reason], evidence)
            report["rejected_scenes"] = rejected
            return report, [], [], [], 0, 0

        burns, scenes, hidden = [], [], []
        seen = total = 0
        discarded_px = 0
        supported: set[int] = set()  # номера детекций, в пятне которых видна гарь
        gap = (post.when.date() - pre.when.date()).days
        weak_night = _weak_night(event, self.config["events"]["weak_night_frp_mw"])
        reliable = gap <= self.config["scenes"]["reliable_gap_days"] and not weak_night
        # Снимки и прогноз сохраняются: по ним редактор доразметки показывает исходную сцену.
        root = self.settings.satellite_cache_dir / "annotation-scenes" / digest / "bs"
        # Проверка времени изменения: снимки между «до» и огнём и между огнём и «после».
        limit = self.config["scenes"].get("timing_check_scenes", 0)
        checks = {side: {"days": days, "used": set(), "burned_px": 0, "checked_px": 0, "removed_px": 0,
                         "error": None}
                  for side, days in (
                      ("before", _days_between(items, pre.when.date(), event.first_detected, _acquired,
                                               limit, newest_first=True)),
                      ("after", _days_between(items, event.last_detected, post.when.date(), _acquired,
                                              limit, newest_first=False)))}
        harvest = {"burned_px": 0, "checked_px": 0, "removed_px": 0, "removed_by_class": {}}
        try:
            for bounds, mask in zip(tiles, masks):
                if not mask.any():
                    continue
                chip_id = bounds["chip_id"]
                bright = self.config["scenes"].get("bright_cloud_b2")
                pre_raster = _mark_bright_clouds(self.backend.scene(bounds, pre.when), bright)
                post_raster = _mark_bright_clouds(self.backend.scene(bounds, post.when), bright)
                aux = self.backend.aux(bounds)
                _write_raster(root / "sentinel2_pre" / f"{chip_id}_pre.tif", pre_raster, bounds)
                _write_raster(root / "sentinel2_post" / f"{chip_id}_post.tif", post_raster, bounds)
                _write_raster(root / "aux" / f"{chip_id}_aux.tif", aux, bounds)
                prediction = self.backend.model(root, bounds, selected)
                prediction = np.where(mask, prediction, 0).astype(np.uint8)
                landcover = np.asarray(aux)[2]
                prediction, brightened = self._harvest_filter(prediction, pre_raster, post_raster, landcover,
                                                              harvest)
                # Что вырезала проверка: картинка «снимки до/после» показывает это отдельным слоем.
                _write_raster(root.parent / "filtered" / f"{chip_id}_harvest.tif",
                              brightened[None, :, :].astype(np.uint8), bounds)
                for side, check in checks.items():
                    prediction = self._timing_filter(prediction, bounds, pre_raster, post_raster, landcover,
                                                     selected, check, side)
                prediction, far, tile_supported = _at_detections(
                    prediction, event, bounds, pixels, gsd, self.config["events"]["detection_radius_m"],
                    self.config["events"]["min_scar_share"])
                discarded_px += far
                supported |= tile_supported
                cloud_classes = self.config["scenes"]["cloud_classes"] + self.config["scenes"]["nodata_classes"]
                observed = ~np.isin(pre_raster[9], cloud_classes) & ~np.isin(post_raster[9], cloud_classes)
                seen += int(np.count_nonzero(observed & mask))
                total += int(np.count_nonzero(mask))
                affine = from_origin(bounds["x_min"], bounds["y_max"], gsd, gsd)
                to_wgs84 = Transformer.from_crs(bounds["epsg"], 4326, always_xy=True).transform
                hidden.extend(_blocks(~observed & mask, affine, to_wgs84))
                _write_raster(root.parent / "predictions" / f"{chip_id}_severity.tif",
                              prediction[None, :, :], bounds)
                # Один контур на степень в плитке: тысяча кусочков одного пожара эксперту не нужна.
                for severity in (1, 2, 3):
                    pieces = [shape(g) for g, value in shapes(prediction, mask=prediction == severity,
                                                                transform=affine) if value == severity]
                    if not pieces:
                        continue
                    polygon = transform(to_wgs84, unary_union(pieces))
                    if polygon.is_empty:
                        continue
                    classes, counts = np.unique(landcover[prediction == severity], return_counts=True)
                    burns.append(Burn(
                        # Дата контура — начало пожара; даты снимков — отдельными полями.
                        id=f"{event.id}-{chip_id}-s{severity}", date=event.first_detected,
                        severity=severity, geometry=polygon, source=f"Sentinel-2 / {selected['model']}",
                        extra={"event_id": event.id, "fire_evidence": event.evidence(),
                               "fire_first_detected": event.first_detected.isoformat(),
                               "fire_last_detected": event.last_detected.isoformat(),
                               "reliable": reliable,
                               "landcover_ha": {LANDCOVER.get(int(c), f"класс {int(c)}"):
                                                round(int(n) * gsd * gsd / 1e4, 2)
                                                for c, n in zip(classes, counts)},
                               "chip_id": chip_id, "prediction_key": digest,
                               "pre_scene_id": pre.item["id"], "post_scene_id": post.item["id"],
                               "pre_image_date": pre.when.date().isoformat(),
                               "post_image_date": post.when.date().isoformat()}))
                _write_metadata(root.parent / "metadata" / f"{chip_id}.json",
                                {**bounds, "chip_id": chip_id, "width": pixels, "height": pixels,
                                 "date_pre": pre.when.date().isoformat(),
                                 "date_post": post.when.date().isoformat(),
                                 "pre_scene_id": pre.item["id"], "post_scene_id": post.item["id"],
                                 "model": selected["model"], "prediction_key": digest,
                                 "event_id": event.id})
                scenes.append({"tile": chip_id, "event_id": event.id, "pre": pre.item["id"],
                               "post": post.item["id"], "pre_date": pre.when.date().isoformat(),
                               "post_date": post.when.date().isoformat(),
                               "area_pixels": int(np.count_nonzero(mask)),
                               "observed_fraction": round(float(np.count_nonzero(observed & mask))
                                                          / max(int(np.count_nonzero(mask)), 1), 4)})
        except ValueError as error:  # данные пришли, но непригодны — это не сбой сети
            return (_event_report(event, "insufficient_data", "scene_unusable",
                                  f"Снимки или вспомогательные слои непригодны для расчёта: {error}", evidence),
                    [], [], [], 0, 0)
        except (OSError, RuntimeError) as error:
            return (_event_report(event, "source_unavailable", "copernicus_unavailable",
                                  f"Не удалось получить снимки или выполнить модель: {error}", evidence),
                    [], [], [], 0, 0)

        report = _event_report(event, "ok", None, None, evidence)
        if not supported:
            report.update(status="no_scar", reason="no_scar_at_detections",
                          message="Огонь зафиксирован, но в местах детекций гарь на снимках не видна "
                                  "(доля гари в пикселе VIIRS ниже порога).")
        report["detections_with_scar"] = len(supported)
        report["pair"] = {"pre": pre.when.date().isoformat(), "post": post.when.date().isoformat(),
                          "cloud_pre": round(pre.cloud, 4), "cloud_post": round(post.cloud, 4),
                          "pre_scene_id": pre.item["id"], "post_scene_id": post.item["id"]}
        report["rejected_scenes"] = rejected
        report["observed_fraction"] = round(seen / total, 4) if total else None
        forward = Transformer.from_crs(4326, event.epsg, always_xy=True).transform
        report["burn_area_ha"] = round(sum(transform(forward, burn.geometry).area for burn in burns) / 1e4, 2)
        report["burn_by_landcover_ha"] = _landcover_of(burns)
        report["area_per_point_ha"] = round(report["burn_area_ha"] / max(len(event.points), 1), 2)
        report["harvest_check"] = self._harvest_report(harvest, gsd)
        report["buffer_edge"] = _buffer_edge(burns, footprint, event.epsg, gsd,
                                             self.config["events"].get("buffer_edge_share", 0.10),
                                             self.config["events"].get("buffer_edge_closing_m", 50))
        reasons = (([f"между снимками {gap} дн."] if gap > self.config["scenes"]["reliable_gap_days"] else [])
                   + (["слабый ночной огонь — вероятно тление"] if weak_night else []))
        check = report["harvest_check"]
        if burns and not check["passed"]:
            season = event.first_detected.month in (9, 10) and _mostly_crops(report["burn_by_landcover_ha"])
            problem = ("проверка «убрано или выжжено» не проведена" if not check["performed"] else
                       f"{check['removed_share'] * 100:.0f}% изменений у очага — уборка (поля посветлели), а не огонь")
            reasons.append(f"сезон уборки на пашне: {problem}" if season else problem)
        if report["buffer_edge"]["touches"]:
            reasons.append(f"контур упирается в границу зоны поиска на "
                           f"{report['buffer_edge']['share'] * 100:.0f}% периметра — гарь не замыкается вокруг очага")
        # Надёжно — только если пара близка по времени, проверка «убрано или выжжено» пройдена и контур
        # не упирается в границу зоны поиска. Разрыв ≤ 15 дней сам по себе ничего не гарантирует.
        reliable = not reasons
        if any(burn.extra.get("reliable") != reliable for burn in burns):
            from dataclasses import replace as replace_field

            burns = [replace_field(burn, extra={**burn.extra, "reliable": reliable}) for burn in burns]
        report["reliability"] = "reliable" if reliable else "unreliable"
        report["reliability_reasons"] = reasons
        report["discarded_far_from_fire_ha"] = round(discarded_px * gsd * gsd / 1e4, 2)
        if discarded_px:
            report["limitations"].append(
                f"{report['discarded_far_from_fire_ha']:.0f} га изменений в буфере не пересекают места "
                "детекций огня с подтверждённой гарью и гарью не считаются (вероятно, уборка или вспашка).")
        if weak_night:
            report["limitations"].append(
                f"Все детекции ночные и слабые (FRP до {event.max_frp:.1f} МВт): вероятно тление (солома, "
                "отходы), а не пожар с заметной гарью; площадь не входит в надёжную.")
        report["pair"]["gap_days"] = gap
        if report["harvest_check"]["removed_ha"] >= 0.5:
            report["limitations"].append(
                f"{report['harvest_check']['removed_ha']:.0f} га посветлели во всех видимых каналах — поле "
                "убрано (или закрыто дымкой), а не выжжено (уголь темнее); гарью не считаются.")
        if report["buffer_edge"]["touches"]:
            report["limitations"].append(
                f"Контур гари упирается в границу зоны поиска (буфер 3 км или край области) на "
                f"{report['buffer_edge']['share'] * 100:.0f}% периметра: гарь не замыкается вокруг очага — "
                "вероятно, захвачены соседние поля; оценка ненадёжна.")
        self._report_timing(report, checks, pre.when.date(), event.first_detected, gsd)
        report["pair"]["reliable"] = gap <= self.config["scenes"]["reliable_gap_days"]
        if report["pair"]["reliable"]:
            report["limitations"].append(f"Между снимками {gap} дн.: за это время возможны уборка, вспашка "
                                         "и восстановление растительности.")
        else:
            report["limitations"].append(
                f"Низкая надёжность: между снимками {gap} дн. (ясных снимков ближе к пожару нет). За это время "
                "соседние поля могли убрать и вспахать — площадь гари, вероятно, завышена.")
        if selected["model"] == "landcover_contour_dnbr":
            report["limitations"].append("Пороги dNBR по типу покрова взяты из методики и в Краснодарском "
                                         "крае не откалиброваны.")
        if report["observed_fraction"] is not None and report["observed_fraction"] < 0.995:
            report["limitations"].append(
                f"{(1 - report['observed_fraction']) * 100:.0f}% буфера закрыто облаками или тенями: там гарь "
                "не оценивалась.")
        return report, burns, scenes, hidden, seen, total

    def _harvest_filter(self, prediction: np.ndarray, pre_raster: np.ndarray, post_raster: np.ndarray,
                        landcover: np.ndarray, harvest: dict) -> tuple[np.ndarray, np.ndarray]:
        """Убрать пиксели, которые посветлели во всех видимых каналах: убрано, а не выжжено.

        Огонь оставляет уголь: синий и зелёный каналы темнеют. Красный у сгоревшей зелени
        (зелёный рис, камыш) может и посветлеть — уходит хлорофилл, поглощавший красный, —
        поэтому один красный канал не признак. Солома и стерня после уборки ярче и зелени,
        и угля во всех трёх видимых каналах. Пиксель считается убранным, только если все три
        (B2, B3, B4) посветлели больше порога `scenes.harvest_brightening` — двух типичных
        погрешностей отражения L2A после атмосферной коррекции (~0,01).

        Только на покровах, где бывают уборка и покос (`scenes.harvest_landcover`: пашня,
        травяные угодья, водно-болотные — рисовые чеки и камыш). Замер на пожарах CLMS
        (experiments/validation/harvest-feature-by-landcover.json): в лесу огонь в кронах
        оставляет бурую сухую листву, светлее зелени во всех каналах (срезалось бы 16 %
        настоящей гари), у кромки воды до огня была тёмная вода (30 %). Уборки там нет,
        и признак «посветлело — убрано» физически неприменим.
        """
        cfg = self.config["scenes"]
        threshold = cfg.get("harvest_brightening")
        burned = prediction > 0
        harvest["burned_px"] += int(np.count_nonzero(burned))
        empty = np.zeros(prediction.shape, bool)
        if not threshold or not burned.any():
            return prediction, empty
        invalid = cfg["cloud_classes"] + cfg["nodata_classes"]
        valid = burned & ~np.isin(pre_raster[9], invalid) & ~np.isin(post_raster[9], invalid)
        covers = cfg.get("harvest_landcover")
        if covers:
            valid &= np.isin(landcover, covers)
        change = post_raster[:3].astype(np.int32) - pre_raster[:3].astype(np.int32)
        brightened = valid & (change.min(axis=0) > threshold * 10000)
        harvest["checked_px"] += int(np.count_nonzero(valid))
        harvest["removed_px"] += int(np.count_nonzero(brightened))
        if brightened.any():
            classes, counts = np.unique(landcover[brightened], return_counts=True)
            for cls, n in zip(classes, counts):
                name = LANDCOVER.get(int(cls), f"класс {int(cls)}")
                harvest["removed_by_class"][name] = harvest["removed_by_class"].get(name, 0) + int(n)
        return np.where(brightened, 0, prediction).astype(np.uint8), brightened

    def _harvest_report(self, harvest: dict, gsd: int) -> dict[str, Any]:
        hectares = lambda px: round(px * gsd * gsd / 1e4, 2)
        share = harvest["checked_px"] / harvest["burned_px"] if harvest["burned_px"] else None
        removed_share = harvest["removed_px"] / harvest["burned_px"] if harvest["burned_px"] else 0.0
        performed = bool(self.config["scenes"].get("harvest_brightening"))
        # Пройдена — если уборкой оказалось меньше половины изменений у очага. Когда поля в основном
        # посветлели, остаток контура тоже под подозрением: это сезон уборки, а не пожар на всей площади.
        return {"performed": performed, "passed": performed and removed_share < 0.5,
                "checked_share": None if share is None else round(share, 4),
                "removed_ha": hectares(harvest["removed_px"]), "removed_share": round(removed_share, 4),
                "removed_by_landcover_ha": {k: hectares(v) for k, v in harvest["removed_by_class"].items()}}

    def _timing_filter(self, prediction: np.ndarray, bounds: dict, pre_raster: np.ndarray,
                       post_raster: np.ndarray, landcover: np.ndarray, selected: dict, check: dict,
                       side: str) -> np.ndarray:
        """Убрать из гари пиксели, потемневшие не тогда, когда горело (уборка, вспашка).

        Между снимками «до» и «после» бывают недели. Промежуточные снимки отвергнуты для
        пары облачностью, но над отдельными пикселями ясны.
        before: самый поздний ясный снимок до огня; если падение NBR от него до снимка
        «после» ниже порога контура, поле стало таким ещё до огня.
        after: самый ранний ясный снимок после огня; если падение NBR от снимка «до» до
        него ниже порога, сразу после огня гари здесь не было — поле потемнело позже.
        Порог — тот же, что у правила гари для этого типа земли.
        """
        burned = prediction > 0
        check["burned_px"] += int(np.count_nonzero(burned))
        if not check["days"] or not burned.any() or check["error"]:
            return prediction
        cfg = self.config["scenes"]
        from scipy import ndimage

        invalid = cfg["cloud_classes"] + cfg["nodata_classes"]
        pixel_m = (bounds["x_max"] - bounds["x_min"]) / prediction.shape[1]
        grow = int(round(cfg.get("timing_cloud_buffer_m", 0) / pixel_m))
        middle = np.full(prediction.shape, np.nan, np.float32)
        try:
            for when in check["days"]:  # от ближайшего к огню
                todo = burned & np.isnan(middle)
                if not todo.any():
                    break
                raster = _mark_bright_clouds(self.backend.scene(bounds, when), cfg.get("bright_cloud_b2"))
                clear_any = ~np.isin(raster[9], invalid)
                if cfg.get("haze_dark_b2") and np.count_nonzero(clear_any) >= 1000 and                         np.percentile(raster[0][clear_any], 5) / 10000 > cfg["haze_dark_b2"]:
                    continue  # дымка: NBR под ней ниже настоящего, сравнение было бы ложным
                # Край облака — дымка, которую SCL не отмечает: NBR под ней выше настоящего,
                # и гарь выглядела бы «ещё не потемневшей». Маска облаков расширяется.
                cloudy = ndimage.binary_dilation(np.isin(raster[9], invalid), iterations=grow) if grow \
                    else np.isin(raster[9], invalid)
                clear = todo & ~cloudy
                if clear.any():
                    middle[clear] = _nbr(raster)[clear]
                    check["used"].add(when.date().isoformat())
        except (OSError, RuntimeError, ValueError) as error:
            check["error"] = str(error)
            return prediction
        checked = burned & ~np.isnan(middle)
        drop = middle - _nbr(post_raster) if side == "before" else _nbr(pre_raster) - middle
        changed = checked & (np.where(checked, drop, np.inf) < _contour_threshold(landcover, selected))
        check["checked_px"] += int(np.count_nonzero(checked))
        check["removed_px"] += int(np.count_nonzero(changed))
        return np.where(changed, 0, prediction).astype(np.uint8)

    @staticmethod
    def _report_timing(report: dict[str, Any], checks: dict, pre_day: date, fire_day: date, gsd: int) -> None:
        for side, check in checks.items():
            hectares = round(check["removed_px"] * gsd * gsd / 1e4, 2)
            share = check["checked_px"] / check["burned_px"] if check["burned_px"] else None
            key = "pre_fire_check" if side == "before" else "post_fire_check"
            report[key] = {"scenes": sorted(check["used"]), "candidate_scenes": len(check["days"]),
                           "checked_share": None if share is None else round(share, 4),
                           "removed_ha": hectares, "error": check["error"]}
            if check["error"]:
                report["limitations"].append(f"Проверка времени изменения поля не удалась: {check['error']}")
            elif hectares >= 0.5 and side == "before":
                report["limitations"].append(
                    f"{hectares:.0f} га потемнели ещё до огня (снимок {max(check['used'])}): поле убрали или "
                    "вспахали раньше пожара — гарью не считаются.")
            elif hectares >= 0.5:
                report["limitations"].append(
                    f"{hectares:.0f} га на снимке сразу после огня ({min(check['used'])}) гарью ещё не были и "
                    "потемнели позже — вероятно, уборка или вспашка; гарью не считаются.")
        days = (fire_day - pre_day).days
        before = checks["before"]
        if not before["error"] and not before["used"] and before["burned_px"] and days > 5:
            report["limitations"].append(
                f"Между снимком «до» и огнём {days} дн., более поздних ясных снимков нет: не проверено, "
                "не убрали ли поле раньше пожара.")

    def _haze(self, tiles: list[dict], scl: list[np.ndarray], when: datetime) -> float | None:
        """Дымка по тёмным объектам: 5-й процентиль B02 на ясных по SCL пикселях плиток.

        Маска SCL пропускает дымку, а под ней NBR падает как на гари. В ясную погоду
        тёмные объекты (вода, густая зелень, тени) в синем канале почти чёрные —
        отражение около 0,01–0,03; дымка поднимает и их. Уборка и вспашка делают ярче
        поля, но не самые тёмные пиксели. None — проверить нечем.
        """
        cfg = self.config["scenes"]
        if not cfg.get("haze_dark_b2") or not hasattr(self.backend, "blue"):
            return None
        invalid = cfg["cloud_classes"] + cfg["nodata_classes"]
        values = [self.backend.blue(bounds, when)[~np.isin(s, invalid)] for bounds, s in zip(tiles, scl)]
        values = np.concatenate(values) if values else np.array([])
        if values.size < 1000:
            return None
        return float(np.percentile(values, 5)) / 10000

    def _pair(self, event: FireEvent, items: list[dict], tiles: list[dict], masks: list[np.ndarray],
              acquired):
        """Пара снимков, пригодная над буфером события; иначе причина отказа."""
        cfg = self.config["scenes"]
        today = datetime.now(timezone.utc).date()
        pre_from = event.first_detected - timedelta(days=cfg["pre_window_days"])
        post_to = min(event.last_detected + timedelta(days=cfg["post_window_days"]), today)
        by_day: dict[date, dict] = {}
        for item in items:
            if item.get("collection", "sentinel-2-l2a") != "sentinel-2-l2a":
                continue
            day = acquired(item).date()
            by_day.setdefault(day, item)  # Process API отдаёт снимок за календарный день
        pre_days = sorted((d for d in by_day if pre_from <= d < event.first_detected), reverse=True)
        post_days = sorted(d for d in by_day if event.last_detected < d <= post_to)
        rejected: list[dict] = []

        def usable(days: list[date], side: str) -> list[Candidate]:
            suitable = []
            for day in days[:cfg["max_candidates"]]:
                when = acquired(by_day[day])
                scl = [self.backend.scl(bounds, when) for bounds in tiles]
                pixels = sum(int(mask.sum()) for mask in masks) or 1
                cloud = sum(int(np.count_nonzero(np.isin(s, cfg["cloud_classes"]) & m))
                            for s, m in zip(scl, masks)) / pixels
                nodata = sum(int(np.count_nonzero(np.isin(s, cfg["nodata_classes"]) & m))
                             for s, m in zip(scl, masks)) / pixels
                if cloud > cfg["max_cloud_fraction"] or nodata > cfg["max_nodata_fraction"]:
                    rejected.append({"side": side, "date": day.isoformat(), "scene_id": by_day[day]["id"],
                                     "cloud": round(cloud, 4), "nodata": round(nodata, 4),
                                     "reason": "too_cloudy" if cloud > cfg["max_cloud_fraction"] else "nodata"})
                    continue
                suitable.append(Candidate(by_day[day], when, cloud, nodata, scl))
            return suitable

        pres = usable(pre_days, "pre")
        if not pres:
            return None, None, rejected, "too_cloudy" if pre_days else "no_pre_scene"
        posts = usable(post_days, "post")
        if not posts:
            return None, None, rejected, "too_cloudy" if post_days else "no_post_scene"
        # Сначала пара, надёжная по разрыву: за недели между снимками поля убирают и вспахивают,
        # и это хуже, чем облачность до max_cloud_fraction, — облачная часть заштрихована и не
        # оценивается. Среди надёжных — менее облачная (шаг cloud_tie_step), затем более
        # короткая. Если надёжной пары нет — самая короткая, при равенстве менее облачная.
        step = cfg["cloud_tie_step"]
        limit = cfg["reliable_gap_days"]

        def order(pair: tuple[Candidate, Candidate]):
            gap = (pair[1].when.date() - pair[0].when.date()).days
            clouds = round(pair[0].cloud / step) + round(pair[1].cloud / step)
            return (gap > limit, gap if gap > limit else 0, clouds, gap)

        # Дымка проверяется лениво — только у снимков лучшей пары: синий канал по всем
        # кандидатам удваивал бы число запросов. Снимок в дымке отбрасывается, пара
        # выбирается заново; итог тот же, что при проверке всех кандидатов.
        while True:
            pre, post = min(((a, b) for a in pres for b in posts), key=order)
            hazy = [(c, side) for c, side in ((pre, "pre"), (post, "post")) if self._hazy(c, tiles)]
            if not hazy:
                return pre, post, rejected, None
            for candidate, side in hazy:
                rejected.append({"side": side, "date": candidate.when.date().isoformat(),
                                 "scene_id": candidate.item["id"], "cloud": round(candidate.cloud, 4),
                                 "nodata": round(candidate.nodata, 4),
                                 "haze_dark_b2": round(candidate.haze, 4), "reason": "haze"})
                (pres if side == "pre" else posts).remove(candidate)
            if not pres or not posts:
                return None, None, rejected, "too_cloudy"

    def _hazy(self, candidate: Candidate, tiles: list[dict]) -> bool:
        if candidate.haze is None:
            candidate.haze = self._haze(tiles, candidate.scl, candidate.when)
        limit = self.config["scenes"].get("haze_dark_b2")
        return candidate.haze is not None and limit is not None and candidate.haze > limit


_REASONS = {
    "no_pre_scene": "Нет снимка Sentinel-2 до первой детекции огня в окне поиска.",
    "no_post_scene": "Нет снимка Sentinel-2 после последней детекции огня в окне поиска "
                     "(возможно, пожар слишком свежий).",
    "too_cloudy": "Все снимки-кандидаты над очагом закрыты облаками или дымкой сильнее допустимого.",
}


_OSM_NOTES = ("Рядом с точками огня по OpenStreetMap", "OpenStreetMap: ближе", "Проверка промышленных объектов")


def _event_report(event: FireEvent, status: str, reason: str | None, message: str | None,
                  evidence) -> dict[str, Any]:
    limitations = [VIIRS_LIMITATION]
    if not evidence.persistent.covers(event.buffer.bounds):
        limitations.append("Маска постоянных тепловых источников для этого района не построена: "
                           "исключены только источники, помеченные FIRMS (тип 2, только архив SP).")
    if any(point.source.endswith("_NRT") for point in event.points):
        limitations.append("Оперативные данные FIRMS (NRT) не содержат пометки статических источников.")
    lon, lat = event.buffer.centroid.x, event.buffer.centroid.y
    return {"event_id": event.id, "status": status, "reason": reason, "message": message,
            "fire_evidence": event.evidence(), "centre": [round(lon, 5), round(lat, 5)],
            "buffer": mapping(event.buffer), "pair": None, "rejected_scenes": [],
            "observed_fraction": None, "burn_area_ha": None, "discarded_far_from_fire_ha": None,
            "reliability": None, "reliability_reasons": [], "burn_by_landcover_ha": {},
            "industrial_context": None, "weather": None, "pre_fire_check": None,
            "post_fire_check": None, "harvest_check": None, "buffer_edge": None, "area_per_point_ha": None,
            "limitations": limitations}


def _hotspot(point, event_id: str) -> Hotspot:
    from shapely.geometry import Point

    return Hotspot(
        id=f"{point.source}-{point.date.isoformat()}-{point.time_utc}-{point.lon:.4f}-{point.lat:.4f}",
        date=point.date, satellite=point.platform, instrument="VIIRS", confidence=point.confidence,
        frp_mw=point.frp_mw, geometry=Point(point.lon, point.lat),
        extra={"time_utc": point.time_utc, "source": point.source, "event_id": event_id})


def utm_epsg_for(area) -> int:
    from service.fire_evidence import utm_epsg

    return utm_epsg(area.centroid.x, area.centroid.y)


def _mask(area, bounds: dict, pixels: int, gsd: int) -> np.ndarray:
    from rasterio.features import geometry_mask
    from rasterio.transform import from_origin

    to_metric = Transformer.from_crs(4326, bounds["epsg"], always_xy=True).transform
    projected = transform(to_metric, area)
    if projected.is_empty:
        return np.zeros((pixels, pixels), bool)
    return geometry_mask([projected], out_shape=(pixels, pixels),
                         transform=from_origin(bounds["x_min"], bounds["y_max"], gsd, gsd), invert=True)


LANDCOVER = {10: "лес", 20: "кустарник", 30: "травяные угодья", 40: "пашня", 50: "застройка",
             60: "голая земля", 70: "снег и лёд", 80: "вода", 90: "водно-болотные угодья",
             95: "мангры", 100: "мхи и лишайники", 0: "нет данных"}


def _dump_dataset(dataset: Dataset) -> dict[str, Any]:
    return {
        "hotspots": [{"id": h.id, "date": h.date.isoformat(), "satellite": h.satellite,
                      "instrument": h.instrument, "confidence": h.confidence, "frp_mw": h.frp_mw,
                      "geometry": mapping(h.geometry), "extra": h.extra} for h in dataset.hotspots],
        "burns": [{"id": b.id, "date": b.date.isoformat(), "severity": b.severity, "source": b.source,
                   "geometry": mapping(b.geometry), "extra": b.extra} for b in dataset.burns],
        "origin": dataset.origin, "files": list(dataset.files), "updated_at": dataset.updated_at,
    }


def _load_dataset(data: dict[str, Any]) -> Dataset:
    hotspots = tuple(Hotspot(h["id"], date.fromisoformat(h["date"]), h["satellite"], h["instrument"],
                             h["confidence"], h["frp_mw"], shape(h["geometry"]), h["extra"])
                     for h in data["hotspots"])
    burns = tuple(Burn(b["id"], date.fromisoformat(b["date"]), b["severity"], shape(b["geometry"]),
                       b["source"], b["extra"]) for b in data["burns"])
    return Dataset(hotspots, burns, data["origin"], tuple(data["files"]), data["updated_at"])


def _mark_bright_clouds(raster: np.ndarray, threshold: float | None) -> np.ndarray:
    """Яркие облака, которые SCL пропустил, пометить облаком (класс 8).

    Sen2Cor иногда относит плотное облако к «голой почве» или «неклассифицированным»
    (классы 5 и 7). Под таким облаком NBR после пожара падает, и без пометки это
    выглядело бы как гарь. Синий канал выше порога у почвы и растительности почти не
    встречается; вода и снег (классы 6 и 11) не трогаются.
    """
    if not threshold:
        return raster
    raster = np.array(raster, copy=True)
    bright = (raster[0] > threshold * 10000) & ~np.isin(raster[9], (0, 1, 3, 6, 8, 9, 10, 11))
    raster[9][bright] = 8
    return raster


def _buffer_edge(burns: list[Burn], footprint, epsg: int, gsd: int, limit: float,
                 closing_m: float = 50) -> dict[str, Any]:
    """Какая доля внешнего обвода гари лежит на границе зоны поиска (буфер 3 км ∩ область).

    Гарь, замкнутая вокруг очага, границы не касается. Если заметная часть обвода лежит
    на ней, пятно продолжается за зоной поиска — обычно это соседние поля, а не огонь.
    Пятна, разделённые валиками, дорогами и каналами уже `closing_m`, сливаются в одно
    (морфологическое замыкание): иначе сотни рисовых чеков дают огромный суммарный
    периметр, и доля на границе выглядит ничтожной. Дыры не учитываются.
    """
    from shapely.geometry import Polygon

    if not burns:
        return {"share": 0.0, "touches": False}
    forward = Transformer.from_crs(4326, epsg, always_xy=True).transform
    scar = unary_union([transform(forward, burn.geometry).buffer(0) for burn in burns])
    scar = scar.buffer(closing_m).buffer(-closing_m) if closing_m else scar
    parts = [Polygon(part.exterior) for part in getattr(scar, "geoms", [scar]) if part.geom_type == "Polygon"]
    outline = unary_union(parts).boundary if parts else None
    if outline is None or outline.length == 0:
        return {"share": 0.0, "touches": False}
    edge = transform(forward, footprint).boundary.buffer(2 * gsd)
    share = outline.intersection(edge).length / outline.length
    return {"share": round(share, 4), "touches": share >= limit}


def _mostly_crops(by_landcover: dict[str, dict[str, float]]) -> bool:
    """Больше половины гари — пашня или водно-болотные угодья (рисовые чеки в WorldCover — в них)."""
    total = sum(sum(row.values()) for row in by_landcover.values())
    crops = sum(sum(by_landcover.get(name, {}).values()) for name in ("пашня", "водно-болотные угодья"))
    return bool(total) and crops * 2 > total


def _nbr(raster: np.ndarray) -> np.ndarray:
    """NBR = (B8A − B12) / (B8A + B12) по снимку с каналами в порядке сборщика."""
    nir, swir = raster[6].astype(np.float32), raster[8].astype(np.float32)
    total = nir + swir
    return np.divide(nir - swir, total, out=np.zeros_like(total), where=total > 0)


def _contour_threshold(landcover: np.ndarray, selected: dict) -> np.ndarray:
    """Порог dNBR контура гари для каждого пикселя — тот же, что у правила по типу покрова."""
    from src.models.burn_severity_calibrated_dnbr import DEFAULT, THRESHOLDS

    params = (selected.get("model_description") or {}).get(selected["model"]) or {}
    scale = float(params.get("scale", 1.0))
    if selected["model"] == "landcover_contour_dnbr":
        scale *= float(params.get("contour_scale", 1.2))
    threshold = np.full(landcover.shape, DEFAULT[0], np.float32)
    for cls, values in THRESHOLDS.items():
        threshold[landcover == cls] = values[0]
    return threshold * scale


def _days_between(items: list[dict], after: date, before: date, acquired, limit: int,
                  newest_first: bool) -> list[datetime]:
    """Снимки Sentinel-2 строго между датами, от ближайшего к огню; не больше `limit`."""
    by_day: dict[date, datetime] = {}
    for item in items:
        if item.get("collection", "sentinel-2-l2a") != "sentinel-2-l2a":
            continue
        when = acquired(item)
        if after < when.date() < before:
            by_day.setdefault(when.date(), when)
    return [by_day[day] for day in sorted(by_day, reverse=newest_first)[:limit]]


def _weak_night(event: FireEvent, frp_limit: float) -> bool:
    frp = [p.frp_mw for p in event.points if p.frp_mw is not None]
    return bool(frp) and all(p.daynight == "N" for p in event.points) and max(frp) < frp_limit


def _landcover_of(burns: list[Burn]) -> dict[str, dict[str, float]]:
    """Площадь гари по типам земель (ESA WorldCover) и степеням, га."""
    table: dict[str, dict[str, float]] = {}
    for burn in burns:
        for name, hectares in (burn.extra.get("landcover_ha") or {}).items():
            row = table.setdefault(name, {"1": 0.0, "2": 0.0, "3": 0.0})
            row[str(burn.severity)] = round(row[str(burn.severity)] + hectares, 2)
    return table


def _sum_landcover(reports) -> dict[str, dict[str, float]]:
    table: dict[str, dict[str, float]] = {}
    for report in reports:
        for name, row in (report.get("burn_by_landcover_ha") or {}).items():
            target = table.setdefault(name, {"1": 0.0, "2": 0.0, "3": 0.0})
            for severity, hectares in row.items():
                target[severity] = round(target[severity] + hectares, 2)
    return table


def _write_metadata(path: Path, metadata: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")


def _without(burns: list[Burn], claimed, epsg: int) -> list[Burn]:
    """Убрать из контуров события то, что уже отнесено к более крупному событию.

    Площадь по типам земель уменьшается пропорционально оставшейся площади контура.
    """
    from dataclasses import replace as replace_field

    forward = Transformer.from_crs(4326, epsg, always_xy=True).transform
    result = []
    for burn in burns:
        geometry = burn.geometry.difference(claimed)
        if geometry.is_empty or geometry.area <= 0:
            continue
        share = transform(forward, geometry).area / max(transform(forward, burn.geometry).area, 1e-9)
        landcover = {name: round(ha * share, 2) for name, ha in (burn.extra.get("landcover_ha") or {}).items()}
        result.append(replace_field(burn, geometry=geometry, extra={**burn.extra, "landcover_ha": landcover}))
    return result


def _at_detections(prediction: np.ndarray, event: FireEvent, bounds: dict, pixels: int, gsd: int,
                   radius_m: float, min_share: float) -> tuple[np.ndarray, int, set[int]]:
    """Гарь, подтверждённая в местах детекций огня.

    Для каждой термоточки берётся круг размером с пиксель VIIRS. Детекция
    подтверждена, если гарь занимает не меньше min_share круга (считается по
    части круга внутри плитки). Остаются связные пятна гари, пересекающие
    подтверждённые круги; возвращаются маска, число отброшенных пикселей и
    номера подтверждённых детекций.
    """
    from rasterio.features import geometry_mask
    from rasterio.transform import from_origin
    from scipy import ndimage
    from shapely.geometry import Point

    burned = prediction > 0
    if not burned.any():
        return prediction, 0, set()
    to_metric = Transformer.from_crs(4326, bounds["epsg"], always_xy=True).transform
    grid = from_origin(bounds["x_min"], bounds["y_max"], gsd, gsd)
    tile = box(bounds["x_min"], bounds["y_min"], bounds["x_max"], bounds["y_max"])
    supported: set[int] = set()
    confirmed = np.zeros_like(burned)
    for index, point in enumerate(event.points):
        circle = Point(*to_metric(point.lon, point.lat)).buffer(radius_m)
        if not circle.intersects(tile):
            continue
        inside = geometry_mask([circle], out_shape=(pixels, pixels), transform=grid, invert=True)
        if inside.any() and np.count_nonzero(burned & inside) / np.count_nonzero(inside) >= min_share:
            supported.add(index)
            confirmed |= inside
    labels, _ = ndimage.label(burned, structure=np.ones((3, 3)))
    keep = np.unique(labels[confirmed & burned])
    kept = np.isin(labels, keep[keep > 0])
    return (np.where(kept, prediction, 0).astype(np.uint8), int(np.count_nonzero(burned & ~kept)),
            supported)


def _blocks(mask: np.ndarray, affine, to_wgs84) -> list:
    from rasterio.features import shapes

    if not mask.any():
        return []
    block = 8
    rows, cols = mask.shape[0] // block, mask.shape[1] // block
    coarse = mask[:rows * block, :cols * block].reshape(rows, block, cols, block).mean(axis=(1, 3)) > 0.5
    coarse_affine = affine * affine.scale(block, block)
    return [transform(to_wgs84, shape(geometry))
            for geometry, value in shapes(coarse.astype(np.uint8), mask=coarse, transform=coarse_affine)
            if value]
