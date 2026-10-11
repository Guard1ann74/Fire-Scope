"""Источник данных: чтение GeoJSON и приведение к внутренней модели.

Единственное место, где сервис знает формат файлов моделей. Читается каталог
с любым числом GeoJSON-файлов; объекты разделяются по признаку `kind`
(`af` — термоточка, `bs` — контур гари), а при его отсутствии — по типу геометрии.
Благодаря этому одинаково подхватываются и отдельные файлы точек и контуров, и один
смешанный `features.geojson`, который пишет `prepare_service.py` ML-части.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from service.config import SEVERITY_LABELS

# Основные имена — те, что пишет src/service/prepare.py. Короткие af/bs остались
# от предыдущей версии ML-части: файлы, созданные до переименования, читаются как прежде.
HOTSPOT_KINDS = {"active_fire", "af", "hotspot", "thermal"}
BURN_KINDS = {"burn_severity", "bs", "burn", "burnscar", "scar"}
_DATE_KEYS = ("date", "acq_date", "acq_datetime", "date_post", "observed_at")
_ID_KEYS = ("id", "contour_id", "point_id", "feature_id", "chip_id")


class DataSourceError(RuntimeError):
    """Данные отсутствуют или повреждены."""


@dataclass(frozen=True)
class Hotspot:
    """Термоточка активного горения."""

    id: str
    date: date
    satellite: str
    instrument: str
    confidence: str | None
    frp_mw: float | None
    geometry: BaseGeometry
    extra: dict[str, Any] = field(default_factory=dict)

    def as_feature(self) -> dict[str, Any]:
        properties = {
            "id": self.id,
            "date": self.date.isoformat(),
            "satellite": self.satellite,
            "instrument": self.instrument,
            "confidence": self.confidence,
            "frp_mw": self.frp_mw,
            **self.extra,
        }
        return {
            "type": "Feature",
            "id": self.id,
            "geometry": {
                "type": "Point",
                "coordinates": [round(self.geometry.x, 6), round(self.geometry.y, 6)],
            },
            "properties": properties,
        }


@dataclass(frozen=True)
class Burn:
    """Контур гари со степенью поражения 1–3."""

    id: str
    date: date
    severity: int
    geometry: BaseGeometry
    source: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def severity_label(self) -> str:
        return SEVERITY_LABELS.get(self.severity, "неизвестна")


@dataclass(frozen=True)
class Dataset:
    """Загруженный слепок данных."""

    hotspots: tuple[Hotspot, ...]
    burns: tuple[Burn, ...]
    origin: str
    files: tuple[str, ...]
    #: Время последней правки файлов — им подписывается «свежесть» выдачи.
    updated_at: str | None = None

    @property
    def date_range(self) -> tuple[str | None, str | None]:
        dates = [item.date for item in self.hotspots] + [item.date for item in self.burns]
        if not dates:
            return None, None
        return min(dates).isoformat(), max(dates).isoformat()


class FeatureStore:
    """Кэш датасета с перечитыванием при изменении файлов на диске.

    Позволяет обновить файлы прогнозов без перезапуска сервиса.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._dataset: Dataset | None = None
        self._stamp: tuple[tuple[str, int, int], ...] | None = None

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def get(self) -> Dataset:
        stamp = self._fingerprint()
        if self._dataset is None or stamp != self._stamp:
            self._dataset = load_dataset(self._data_dir)
            self._stamp = stamp
        return self._dataset

    def reload(self) -> Dataset:
        self._dataset = None
        self._stamp = None
        return self.get()

    def _fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        entries = []
        for path in sorted(self._data_dir.glob("*.geojson")):
            stat = path.stat()
            entries.append((path.name, stat.st_size, stat.st_mtime_ns))
        return tuple(entries)


def load_dataset(data_dir: Path) -> Dataset:
    """Прочитать все `*.geojson` из каталога и разложить по типам объектов."""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise DataSourceError(
            f"Каталог данных не найден: {data_dir}. "
            "Укажите существующий каталог с прогнозами в FIRES_DATA_DIR или --data-dir."
        )
    paths = sorted(data_dir.glob("*.geojson"))
    if not paths:
        raise DataSourceError(f"В каталоге {data_dir} нет файлов *.geojson")

    hotspots: list[Hotspot] = []
    burns: list[Burn] = []
    for path in paths:
        for feature in _iter_features(path):
            record = _normalise(feature, path.stem, len(hotspots) + len(burns) + 1)
            if isinstance(record, Hotspot):
                hotspots.append(record)
            elif isinstance(record, Burn):
                burns.append(record)

    hotspots.sort(key=lambda item: (item.date, item.id))
    burns.sort(key=lambda item: (item.date, item.id))
    newest = max(path.stat().st_mtime for path in paths)
    return Dataset(
        hotspots=tuple(hotspots),
        burns=tuple(burns),
        origin=str(data_dir),
        files=tuple(path.name for path in paths),
        updated_at=datetime.fromtimestamp(newest, tz=timezone.utc).replace(microsecond=0).isoformat(),
    )


def _iter_features(path: Path) -> Iterator[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DataSourceError(f"{path.name}: некорректный JSON ({error})") from error
    except OSError as error:
        raise DataSourceError(f"{path.name}: файл недоступен ({error})") from error
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        features = payload.get("features")
    elif isinstance(payload, list):
        features = payload
    else:
        raise DataSourceError(f"{path.name}: ожидается FeatureCollection")
    if not isinstance(features, list):
        raise DataSourceError(f"{path.name}: поле features должно быть списком")
    for feature in features:
        if isinstance(feature, dict) and feature.get("geometry"):
            yield feature


def _normalise(feature: dict[str, Any], origin: str, index: int) -> Hotspot | Burn | None:
    properties = feature.get("properties") or {}
    geometry = shape(feature["geometry"])
    if geometry.is_empty:
        return None
    kind = str(properties.get("kind", "")).lower()
    if kind in HOTSPOT_KINDS or (not kind and geometry.geom_type == "Point"):
        return _hotspot(feature, properties, geometry, origin, index)
    if kind in BURN_KINDS or (not kind and geometry.geom_type in ("Polygon", "MultiPolygon")):
        return _burn(feature, properties, geometry, origin, index)
    return None


def _hotspot(
    feature: dict[str, Any],
    properties: dict[str, Any],
    geometry: BaseGeometry,
    origin: str,
    index: int,
) -> Hotspot:
    if geometry.geom_type != "Point":
        geometry = geometry.representative_point()
    return Hotspot(
        id=_identifier(feature, properties, f"{origin}-af-{index:04d}"),
        date=_parse_date(properties, origin),
        satellite=str(properties.get("satellite") or properties.get("platform") or "не указан"),
        instrument=str(properties.get("instrument") or properties.get("sensor") or "VIIRS"),
        confidence=_optional_str(properties.get("confidence")),
        frp_mw=_optional_float(properties.get("frp_mw") or properties.get("frp")),
        geometry=geometry,
        extra=_extra(properties, {"kind", "satellite", "platform", "instrument", "sensor",
                                  "confidence", "frp_mw", "frp", *_DATE_KEYS, *_ID_KEYS}),
    )


def _burn(
    feature: dict[str, Any],
    properties: dict[str, Any],
    geometry: BaseGeometry,
    origin: str,
    index: int,
) -> Burn:
    raw_severity = properties.get("severity", properties.get("class_id"))
    try:
        severity = int(raw_severity)
    except (TypeError, ValueError) as error:
        raise DataSourceError(
            f"{origin}: у контура гари нет корректной степени поражения (severity/class_id): {raw_severity!r}"
        ) from error
    if severity not in SEVERITY_LABELS:
        raise DataSourceError(f"{origin}: степень поражения {severity} вне диапазона 1–3")
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    return Burn(
        id=_identifier(feature, properties, f"{origin}-bs-{index:04d}"),
        date=_parse_date(properties, origin),
        severity=severity,
        geometry=geometry,
        source=str(properties.get("source") or properties.get("mission") or "Sentinel-2"),
        extra=_extra(properties, {"kind", "severity", "class_id", "source", "mission",
                                  "area_ha", "severity_label", *_DATE_KEYS, *_ID_KEYS}),
    )


def _identifier(feature: dict[str, Any], properties: dict[str, Any], fallback: str) -> str:
    for key in _ID_KEYS:
        value = properties.get(key)
        if value not in (None, ""):
            return str(value)
    value = feature.get("id")
    return str(value) if value not in (None, "") else fallback


def _parse_date(properties: dict[str, Any], origin: str) -> date:
    for key in _DATE_KEYS:
        raw = properties.get(key)
        if raw in (None, ""):
            continue
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    raise DataSourceError(f"{origin}: у объекта нет распознаваемой даты (ожидается {'/'.join(_DATE_KEYS)})")


def _extra(properties: dict[str, Any], consumed: set[str]) -> dict[str, Any]:
    return {key: value for key, value in properties.items() if key not in consumed}


def _optional_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _optional_float(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None
