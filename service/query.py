"""Пространственно-временная выборка и аналитическая справка."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep

from service.config import SEVERITY_COLORS, SEVERITY_LABELS
from service.geo import area_ha, metric_crs
from service.sources import Dataset, Hotspot

#: Округление координат при выдаче: ~0.1 м, этого достаточно и заметно облегчает ответ.
COORD_PRECISION = 6


class QueryError(ValueError):
    """Некорректные параметры запроса. Наверх уходит как HTTP 400."""


@dataclass(frozen=True)
class QuerySpec:
    """Разобранные и проверенные параметры запроса."""

    area: BaseGeometry
    date_from: date
    date_to: date
    source: str  # "bbox" или "polygon"


def parse_dates(raw_from: str, raw_to: str, max_period_days: int) -> tuple[date, date]:
    date_from = _parse_one(raw_from, "from")
    date_to = _parse_one(raw_to, "to")
    if date_from > date_to:
        raise QueryError("Начало периода позже конца")
    span = (date_to - date_from).days + 1
    if span > max_period_days:
        raise QueryError(f"Период {span} дн. длиннее предела {max_period_days} дн.")
    return date_from, date_to


def _parse_one(raw: str, name: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except (AttributeError, ValueError) as error:
        raise QueryError(f"{name}: ожидается дата в формате ГГГГ-ММ-ДД, получено {raw!r}") from error


def run_query(dataset: Dataset, spec: QuerySpec, *, filter_dates: bool = True) -> dict[str, Any]:
    """Отобрать термоточки и контуры гари, обрезать контуры областью, посчитать справку."""
    crs = metric_crs(spec.area)
    prepared = prep(spec.area)

    hotspots = [
        item
        for item in dataset.hotspots
        if (not filter_dates or spec.date_from <= item.date <= spec.date_to)
        and prepared.intersects(item.geometry)
    ]

    burns: list[dict[str, Any]] = []
    for burn in dataset.burns:
        if filter_dates and not (spec.date_from <= burn.date <= spec.date_to):
            continue
        if not prepared.intersects(burn.geometry):
            continue
        clipped = burn.geometry.intersection(spec.area)
        if clipped.is_empty:
            continue
        hectares = area_ha(clipped, crs)
        if hectares <= 0.0:
            continue
        burns.append(
            {
                "type": "Feature",
                "id": burn.id,
                "geometry": _round_geometry(mapping(clipped)),
                "properties": {
                    "id": burn.id,
                    "severity": burn.severity,
                    "severity_label": burn.severity_label,
                    "area_ha": round(hectares, 2),
                    "date": burn.date.isoformat(),
                    "source": burn.source,
                    "color": SEVERITY_COLORS[burn.severity],
                    "clipped": not burn.geometry.within(spec.area),
                    **burn.extra,
                },
            }
        )

    summary = build_summary(hotspots, burns, spec, crs)
    return {
        "query": {
            "from": spec.date_from.isoformat(),
            "to": spec.date_to.isoformat(),
            "area_source": spec.source,
            "area_ha": round(area_ha(spec.area, crs), 2),
            "bbox": [round(value, COORD_PRECISION) for value in spec.area.bounds],
            "metric_crs": crs,
        },
        "hotspots": {
            "type": "FeatureCollection",
            "features": [item.as_feature() for item in hotspots],
        },
        "burns": {"type": "FeatureCollection", "features": burns},
        "summary": summary,
    }


def build_summary(
    hotspots: list[Hotspot],
    burns: list[dict[str, Any]],
    spec: QuerySpec,
    crs: str,
) -> dict[str, Any]:
    """Суммарная площадь гари и распределение по трём степеням поражения."""
    total = round(float(sum(item["properties"]["area_ha"] for item in burns)), 2)
    by_severity = []
    for severity, label in SEVERITY_LABELS.items():
        selected = [item for item in burns if item["properties"]["severity"] == severity]
        area = round(float(sum(item["properties"]["area_ha"] for item in selected)), 2)
        by_severity.append(
            {
                "severity": severity,
                "label": label,
                "color": SEVERITY_COLORS[severity],
                "count": len(selected),
                "area_ha": area,
                "share": round(area / total, 4) if total else 0.0,
            }
        )
    satellites = sorted({item.satellite for item in hotspots})
    dates = [item.date for item in hotspots] + [
        date.fromisoformat(item["properties"]["date"]) for item in burns
    ]
    return {
        "hotspot_count": len(hotspots),
        "burn_count": len(burns),
        "total_area_ha": total,
        "by_severity": by_severity,
        "satellites": satellites,
        "observed_range": {
            "from": min(dates).isoformat() if dates else None,
            "to": max(dates).isoformat() if dates else None,
        },
        "period": {"from": spec.date_from.isoformat(), "to": spec.date_to.isoformat()},
        "metric_crs": crs,
        "empty": not hotspots and not burns,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def _round_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    return {**geometry, "coordinates": _round_coords(geometry["coordinates"])}


def _round_coords(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (int, float)):
            return [round(float(number), COORD_PRECISION) for number in value]
        return [_round_coords(item) for item in value]
    return value
