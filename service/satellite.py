"""On-demand Sentinel-2 inference for a selected area and event interval.

The catalogue and Process API code is shared with the offline collectors. Raster
chips are temporary; only the vector prediction is returned to the map.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any

from pyproj import Transformer
import numpy as np
from shapely.geometry import box, mapping, shape
from shapely.ops import transform, unary_union

from service.config import Settings
from service.query import QueryError, QuerySpec
from service.sources import Burn, DataSourceError, Dataset

MAX_EVENT_DAYS = 31
MAX_TILES = 4
SCENE_WINDOW_DAYS = 30
#: Облачность в каталоге описывает всю гранулу ~110 км; участок проверяется
#: отдельно по маске SCL, поэтому здесь фильтр мягкий.
SCENE_MAX_CLOUD = 80.0
#: Сколько ближайших дат пробовать с каждой стороны события.
MAX_SCENE_ATTEMPTS = 4
#: Снимок принимается сразу, если на участке ясно не меньше этой доли пикселей.
MIN_CLEAR_FRACTION = 0.8
#: Классы SCL, при которых поверхность не видна: те же, что отбрасывают модели гари.
SCL_UNOBSERVED = (0, 1, 3, 8, 9, 10)


class SatellitePredictor:
    """Cache completed predictions and serialize expensive satellite requests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: OrderedDict[str, tuple[Dataset, dict[str, Any]]] = OrderedDict()
        self._lock = Lock()

    @property
    def configured(self) -> bool:
        return bool(self.settings.cdse_client_id and self.settings.cdse_client_secret)

    def model_problem(self) -> str | None:
        """Почему модель гари не сможет работать; None — всё на месте.

        Проверяется при запуске: конфиг должен существовать и выбирать модель из реестра.
        """
        if not self.settings.model_config.is_file():
            return f"Конфигурация модели не найдена: {self.settings.model_config}"
        from src.config import load_config

        try:
            load_config(self.settings.model_config)
        except ValueError as error:
            return f"Конфигурация модели некорректна: {error}"
        return None

    def predict(self, spec: QuerySpec) -> tuple[Dataset, dict[str, Any]]:
        if not self.configured:
            raise DataSourceError(
                "Для прогноза нужны CDSE_CLIENT_ID и CDSE_CLIENT_SECRET "
                "или локальный файл .secrets/cdse.env"
            )
        days = (spec.date_to - spec.date_from).days + 1
        if days > MAX_EVENT_DAYS:
            raise QueryError(
                f"Для прогноза по снимкам выберите период не длиннее {MAX_EVENT_DAYS} дня. "
                "Это интервал события, а не диапазон поиска в архиве."
            )
        if spec.date_to >= date.today():
            raise QueryError("Нужен снимок после события: выберите дату окончания раньше сегодняшней")
        problem = self.model_problem()
        if problem:
            raise DataSourceError(problem)

        # Reject a whole-map selection before the chip planner enumerates it.
        from src.collector.burn_severity import TILE_METRES, plan_chips

        bounds = spec.area.bounds
        center_lon = (bounds[0] + bounds[2]) / 2
        center_lat = (bounds[1] + bounds[3]) / 2
        zone = min(max(int((center_lon + 180) // 6) + 1, 1), 60)
        epsg = (32600 if center_lat >= 0 else 32700) + zone
        to_metric = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
        projected_bounds = transform(to_metric, box(*bounds)).bounds
        # Сетка плиток начинается от угла участка: до 2 плиток (~20 км) по стороне — всегда 2×2.
        side = 2 * TILE_METRES
        if (projected_bounds[2] - projected_bounds[0] > side or
                projected_bounds[3] - projected_bounds[1] > side):
            raise QueryError(
                f"Участок {(projected_bounds[2] - projected_bounds[0]) / 1000:.1f} × "
                f"{(projected_bounds[3] - projected_bounds[1]) / 1000:.1f} км больше допустимого: "
                f"за один запрос обрабатывается участок до {side / 1000:.1f} км по каждой стороне")
        _, tiles = plan_chips(bounds, anchor="area")
        if not tiles or len(tiles) > MAX_TILES:
            raise QueryError(
                f"Выберите меньшую область: за один запрос обрабатывается до {MAX_TILES} "
                "плиток Sentinel-2 размером 10 × 10 км"
            )
        digest = hashlib.sha256(
            spec.area.wkb + spec.date_from.isoformat().encode() +
            spec.date_to.isoformat().encode() +
            self.settings.model_config.read_bytes()
        ).hexdigest()
        with self._lock:
            if digest in self._cache:
                self._cache.move_to_end(digest)
                return self._cache[digest]
            result = self._compute(spec, tiles, digest)
            self._cache[digest] = result
            if len(self._cache) > 8:
                self._cache.popitem(last=False)
            return result

    def _compute(self, spec: QuerySpec, tiles: list[dict], digest: str) -> tuple[Dataset, dict[str, Any]]:
        try:
            from rasterio.features import shapes
            from rasterio.transform import from_origin
            from src.collector.burn_severity import (
                GSD, PIXELS, _acquired, _scene_raster, _token, _write_raster,
            )
            from src.collector.copernicus import search_scenes
            from src.collector.enrichment import _aux, _geographic_polygon
            from src.collector.pairs import sentinel2_candidates
            from src.config import load_config
            from src.dataset.chips import Chip
            from src.pipeline import predict
        except ImportError as error:
            raise DataSourceError(
                "Для запуска модели установите зависимости из requirements-all.txt"
            ) from error

        burns: list[Burn] = []
        scenes: list[dict[str, Any]] = []
        unobserved: list[Any] = []
        search_from = spec.date_from - timedelta(days=SCENE_WINDOW_DAYS)
        search_to = spec.date_to + timedelta(days=SCENE_WINDOW_DAYS)

        try:
            config = load_config(self.settings.model_config)
            selected = config["burn_severity"]
            model_name = selected["model"]
            token = _token(self.settings.cdse_client_id, self.settings.cdse_client_secret)
            with TemporaryDirectory(prefix="fires-satellite-") as temporary:
                root = Path(temporary) / "bs"
                for bounds in tiles:
                    footprint = _geographic_polygon(bounds)
                    catalogue = search_scenes(
                        mapping(footprint), search_from, search_to,
                        "sentinel-2-l2a", limit=100, max_pages=5,
                    )
                    before, after = sentinel2_candidates(
                        catalogue["items"], footprint.bounds, spec.date_from, spec.date_to,
                        max_cloud=SCENE_MAX_CLOUD, window_days=SCENE_WINDOW_DAYS,
                    )
                    if not before or not after:
                        raise QueryError(
                            "Для выбранной области нет снимков Sentinel-2 и до, и после "
                            f"события в пределах {SCENE_WINDOW_DAYS} дней"
                        )
                    inside = _area_mask(spec.area, bounds, PIXELS, GSD)
                    fetch = lambda when: _scene_raster(token, bounds, when)
                    pre, pre_raster = _clearest(before, fetch, inside, _acquired)
                    post, post_raster = _clearest(after, fetch, inside, _acquired)
                    pre_time, post_time = _acquired(pre), _acquired(post)
                    affine = from_origin(bounds["x_min"], bounds["y_max"], GSD, GSD)
                    to_wgs84 = Transformer.from_crs(
                        f"EPSG:{bounds['epsg']}", "EPSG:4326", always_xy=True,
                    ).transform
                    chip_id = bounds["chip_id"]
                    observed = (~np.isin(pre_raster[9], SCL_UNOBSERVED) &
                                ~np.isin(post_raster[9], SCL_UNOBSERVED))
                    _write_raster(root / "sentinel2_pre" / f"{chip_id}_pre.tif", pre_raster, bounds)
                    _write_raster(root / "sentinel2_post" / f"{chip_id}_post.tif", post_raster, bounds)
                    auxiliary, _ = _aux(bounds, self.settings.satellite_cache_dir)
                    _write_raster(root / "aux" / f"{chip_id}_aux.tif", auxiliary, bounds)
                    chip = Chip(chip_id, "burn_severity", PIXELS, PIXELS, root,
                                {key: str(value) for key, value in bounds.items()})
                    mask = predict(chip, selected)
                    # Сохраняем ровно те чипы, по которым получен прогноз: правки
                    # должны быть привязаны к исходной пиксельной сетке модели.
                    annotation_root = self.settings.satellite_cache_dir / "annotation-scenes" / digest
                    for folder, suffix in (("sentinel2_pre", "pre"),
                                           ("sentinel2_post", "post"), ("aux", "aux")):
                        source = root / folder / f"{chip_id}_{suffix}.tif"
                        destination = annotation_root / "bs" / folder / source.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                    _write_raster(annotation_root / "predictions" / f"{chip_id}_severity.tif",
                                  mask[None, :, :], bounds)
                    metadata = {**bounds, "chip_id": chip_id, "width": PIXELS, "height": PIXELS,
                                "date_pre": pre_time.date().isoformat(),
                                "date_post": post_time.date().isoformat(),
                                "pre_scene_id": pre["id"], "post_scene_id": post["id"],
                                "model": model_name, "prediction_key": digest}
                    meta_path = annotation_root / "metadata" / f"{chip_id}.json"
                    meta_path.parent.mkdir(parents=True, exist_ok=True)
                    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                    for index, (geometry, severity) in enumerate(
                        shapes(mask, mask=mask > 0, transform=affine), start=1
                    ):
                        projected = shape(geometry)
                        if projected.is_empty:
                            continue
                        burns.append(Burn(
                            id=f"{chip_id}-{index}", date=post_time.date(),
                            severity=int(severity),
                            geometry=transform(to_wgs84, projected),
                            source=f"Sentinel-2 / {model_name}",
                            extra={"chip_id": chip_id, "prediction_key": digest,
                                   "pre_scene_id": pre["id"], "post_scene_id": post["id"],
                                   "pre_image_date": pre_time.date().isoformat(),
                                   "post_image_date": post_time.date().isoformat()},
                        ))
                    area_pixels = int(np.count_nonzero(inside))
                    observed_pixels = int(np.count_nonzero(observed & inside))
                    scenes.append({"tile": chip_id, "pre": pre["id"], "post": post["id"],
                                   "pre_date": pre_time.date().isoformat(),
                                   "post_date": post_time.date().isoformat(),
                                   "area_pixels": area_pixels,
                                   "observed_fraction": round(observed_pixels / area_pixels, 4)
                                   if area_pixels else None})
                    unobserved.extend(_unobserved_polygons(~observed & inside, affine, to_wgs84))
        except QueryError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise DataSourceError(f"Не удалось получить снимки или выполнить модель: {error}") from error

        stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        dataset = Dataset(
            hotspots=(), burns=tuple(burns), origin="Copernicus Data Space Process API",
            files=tuple(scene["post"] for scene in scenes), updated_at=stamp,
        )
        total = sum(scene["area_pixels"] for scene in scenes)
        seen = sum(scene["area_pixels"] * (scene["observed_fraction"] or 0) for scene in scenes)
        hidden = unary_union(unobserved).intersection(spec.area) if unobserved else None
        return dataset, {"provider": "Copernicus Data Space", "model": model_name,
                         "scenes": scenes, "generated_at": stamp,
                         # Доля участка, где поверхность видна на обоих снимках. Вне её
                         # гарь не оценивалась: «нет контуров» там не значит «не горело».
                         "observed_fraction": round(seen / total, 4) if total else None,
                         "unobserved": mapping(hidden) if hidden is not None and not hidden.is_empty
                         else None}


def _area_mask(area, bounds: dict, pixels: int, gsd: int) -> np.ndarray:
    """Пиксели плитки, попадающие в выбранную пользователем область."""
    from rasterio.features import geometry_mask
    from rasterio.transform import from_origin

    to_metric = Transformer.from_crs("EPSG:4326", f"EPSG:{bounds['epsg']}", always_xy=True).transform
    return geometry_mask([transform(to_metric, area)], out_shape=(pixels, pixels),
                         transform=from_origin(bounds["x_min"], bounds["y_max"], gsd, gsd),
                         invert=True)


def _clearest(candidates: list[dict], fetch, inside: np.ndarray, acquired) -> tuple[dict, np.ndarray]:
    """Ближайший к событию снимок, на котором участок достаточно ясен.

    Пробуется до MAX_SCENE_ATTEMPTS разных дат; если ни одна не даёт
    MIN_CLEAR_FRACTION, берётся самая ясная, а доля видимой поверхности
    сообщается в ответе.
    """
    best: tuple[float, dict, np.ndarray] | None = None
    tried: set[str] = set()
    pixels = max(int(np.count_nonzero(inside)), 1)
    for item in candidates:
        day = acquired(item).date().isoformat()
        if day in tried:  # Process API отдаёт снимок за календарный день: дубли гранул не нужны.
            continue
        if len(tried) >= MAX_SCENE_ATTEMPTS:
            break
        tried.add(day)
        try:
            raster = fetch(acquired(item))
        except ValueError:  # край полосы съёмки: на участке мало данных
            continue
        clear = np.count_nonzero(~np.isin(raster[9], SCL_UNOBSERVED) & inside) / pixels
        if best is None or clear > best[0]:
            best = (clear, item, raster)
        if clear >= MIN_CLEAR_FRACTION:
            break
    if best is None:
        raise QueryError("Не удалось получить ни одного снимка Sentinel-2 участка рядом с событием")
    return best[1], best[2]


def _unobserved_polygons(mask: np.ndarray, affine, to_wgs84) -> list[Any]:
    """Невидимая часть участка грубыми полигонами (блоки 8×8 пикселей ≈ 160 м)."""
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
