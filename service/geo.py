"""Область запроса: разбор, проверка и площадь в метрической проекции.

Площадь никогда не считается в градусах: геометрия переводится в UTM-зону по центру
области, а для областей шире одной зоны — в равновеликую EPSG:6933.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

WGS84 = "EPSG:4326"
#: Запасная равновеликая проекция для областей шире одной UTM-зоны или у полюсов.
EQUAL_AREA_CRS = "EPSG:6933"
MAX_UTM_SPAN_DEG = 6.0
MAX_UTM_LAT = 80.0
_POLYGON_TYPES = ("Polygon", "MultiPolygon")


class GeometryError(ValueError):
    """Некорректная область запроса. Наверх уходит как HTTP 400."""


def parse_bbox(raw: str) -> BaseGeometry:
    """`minLon,minLat,maxLon,maxLat` → прямоугольник."""
    parts = [part.strip() for part in str(raw).split(",")]
    if len(parts) != 4:
        raise GeometryError("bbox: нужно четыре числа minLon,minLat,maxLon,maxLat")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(part) for part in parts)
    except ValueError as error:
        raise GeometryError("bbox: координаты должны быть числами") from error
    for lon in (min_lon, max_lon):
        if not -180.0 <= lon <= 180.0:
            raise GeometryError("bbox: долгота вне диапазона −180…180")
    for lat in (min_lat, max_lat):
        if not -85.0 <= lat <= 85.0:
            raise GeometryError("bbox: широта вне диапазона −85…85")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise GeometryError("bbox: минимальный угол должен быть левее и ниже максимального")
    return box(min_lon, min_lat, max_lon, max_lat)


def parse_geometry(payload: Any, max_vertices: int) -> BaseGeometry:
    """GeoJSON-полигон (Feature, FeatureCollection или голая геометрия) → shapely."""
    geometry = _unwrap(payload)
    if not isinstance(geometry, dict):
        raise GeometryError("Ожидается объект GeoJSON")
    kind = geometry.get("type")
    if kind not in _POLYGON_TYPES:
        raise GeometryError(f"Ожидается Polygon или MultiPolygon, получено {kind!r}")
    try:
        result = shape(geometry)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise GeometryError(f"Не удалось разобрать геометрию: {error}") from error
    _check_coordinates(result, max_vertices)
    if not result.is_valid:
        # Самопересечение спрямляем нулевым буфером: рисование мышью часто даёт «восьмёрки».
        result = result.buffer(0)
    if result.is_empty or not result.is_valid:
        raise GeometryError("Границы области пересекаются или площадь нулевая")
    return result


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("type") == "Feature":
        return payload.get("geometry")
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        features = payload.get("features") or []
        if len(features) != 1:
            raise GeometryError("FeatureCollection должен содержать ровно один объект")
        return _unwrap(features[0])
    return payload


def _check_coordinates(geometry: BaseGeometry, max_vertices: int) -> None:
    total = 0
    for lon, lat in _iter_coords(geometry):
        if not -180.0 <= lon <= 180.0 or not -85.0 <= lat <= 85.0:
            raise GeometryError("Координаты вне допустимого диапазона")
        total += 1
        if total > max_vertices:
            raise GeometryError(f"Слишком много вершин: предел {max_vertices}")
    if total < 4:
        raise GeometryError("Нужно минимум три вершины")


def _iter_coords(geometry: BaseGeometry):
    polygons = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
    for polygon in polygons:
        yield from polygon.exterior.coords
        for ring in polygon.interiors:
            yield from ring.coords


def metric_crs(geometry: BaseGeometry) -> str:
    """UTM-зона по центру области; для широких областей — равновеликая проекция."""
    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    if max_lon - min_lon > MAX_UTM_SPAN_DEG or max(abs(min_lat), abs(max_lat)) > MAX_UTM_LAT:
        return EQUAL_AREA_CRS
    lon = (min_lon + max_lon) / 2.0
    lat = (min_lat + max_lat) / 2.0
    zone = min(max(int((lon + 180.0) // 6.0) + 1, 1), 60)
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


@lru_cache(maxsize=64)
def _to_metric(crs: str) -> Callable:
    return Transformer.from_crs(WGS84, crs, always_xy=True).transform


def area_ha(geometry: BaseGeometry, crs: str) -> float:
    """Площадь в гектарах в указанной метрической проекции."""
    if geometry.is_empty:
        return 0.0
    return transform(_to_metric(crs), geometry).area / 10_000.0
