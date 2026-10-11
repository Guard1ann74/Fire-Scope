"""Выгрузка результатов: GeoJSON, JSON и CSV."""

from __future__ import annotations

import csv
import io
from typing import Any

#: Атрибуты контура в выгрузке GeoJSON — ровно то, что требует постановка задачи.
BURN_EXPORT_KEYS = ("id", "severity", "area_ha")
HOTSPOT_EXPORT_KEYS = ("id", "date", "satellite", "instrument", "confidence", "frp_mw")

CRS_4326 = {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}


def burns_geojson(result: dict[str, Any], *, full_properties: bool = False) -> dict[str, Any]:
    """Контуры гари в EPSG:4326 с атрибутами id, severity, area_ha."""
    features = []
    for feature in result["burns"]["features"]:
        properties = feature["properties"]
        exported = dict(properties) if full_properties else {
            key: properties[key] for key in BURN_EXPORT_KEYS
        }
        if not full_properties:
            exported["severity_label"] = properties["severity_label"]
            exported["date"] = properties["date"]
        features.append({"type": "Feature", "id": feature["id"], "geometry": feature["geometry"],
                         "properties": exported})
    return _collection(features, result)


def hotspots_geojson(result: dict[str, Any]) -> dict[str, Any]:
    """Термоточки в EPSG:4326."""
    features = [
        {
            "type": "Feature",
            "id": feature["id"],
            "geometry": feature["geometry"],
            "properties": {key: feature["properties"].get(key) for key in HOTSPOT_EXPORT_KEYS},
        }
        for feature in result["hotspots"]["features"]
    ]
    return _collection(features, result)


def _collection(features: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": "fire_monitoring",
        "crs": CRS_4326,
        "metadata": {
            "period": result["summary"]["period"],
            "metric_crs": result["summary"]["metric_crs"],
            "generated_at": result["summary"]["generated_at"],
            "data_source": result.get("data_source"),
            "observed_fraction": (result.get("prediction") or {}).get("observed_fraction"),
        },
        "features": features,
    }


def report_json(result: dict[str, Any]) -> dict[str, Any]:
    """Аналитическая справка без геометрии."""
    summary = result["summary"]
    return {
        "query": result["query"],
        "data_source": result.get("data_source"),
        # Доля участка, видимая на снимках; вне её гарь не оценивалась.
        "observed_fraction": (result.get("prediction") or {}).get("observed_fraction"),
        "unobserved": (result.get("prediction") or {}).get("unobserved"),
        "hotspot_count": summary["hotspot_count"],
        "burn_count": summary["burn_count"],
        "total_area_ha": summary["total_area_ha"],
        "by_severity": summary["by_severity"],
        "satellites": summary["satellites"],
        "observed_range": summary["observed_range"],
        "metric_crs": summary["metric_crs"],
        "empty": summary["empty"],
        "generated_at": summary["generated_at"],
        "contours": [
            {
                "id": feature["properties"]["id"],
                "severity": feature["properties"]["severity"],
                "severity_label": feature["properties"]["severity_label"],
                "area_ha": feature["properties"]["area_ha"],
                "date": feature["properties"]["date"],
            }
            for feature in result["burns"]["features"]
        ],
    }


def report_csv(result: dict[str, Any]) -> str:
    """Справка в CSV: блок сводки, распределение по степеням, перечень контуров.

    Разделитель — точка с запятой, кодировка записывается с BOM в `app.py`,
    чтобы файл открывался в Excel с русской локалью без плясок.
    """
    summary = result["summary"]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")

    writer.writerow(["раздел", "показатель", "значение"])
    source = result.get("data_source")
    if source:
        writer.writerow(["данные", "происхождение", source["description"]])
        writer.writerow(["данные", "синтетические", "да" if source["synthetic"] else "нет"])
    observed = (result.get("prediction") or {}).get("observed_fraction")
    if observed is not None:
        writer.writerow(["наблюдение", "видимая_доля_участка", _num(observed)])
    writer.writerow(["период", "начало", summary["period"]["from"]])
    writer.writerow(["период", "конец", summary["period"]["to"]])
    writer.writerow(["область", "источник", result["query"]["area_source"]])
    writer.writerow(["область", "bbox", ",".join(str(value) for value in result["query"]["bbox"])])
    writer.writerow(["область", "площадь_запроса_га", _num(result["query"]["area_ha"])])
    writer.writerow(["итог", "термоточек", summary["hotspot_count"]])
    writer.writerow(["итог", "контуров_гари", summary["burn_count"]])
    writer.writerow(["итог", "суммарная_площадь_га", _num(summary["total_area_ha"])])
    writer.writerow(["итог", "проекция_расчёта_площади", summary["metric_crs"]])
    writer.writerow([])

    writer.writerow(["степень", "название", "контуров", "площадь_га", "доля_процент"])
    for item in summary["by_severity"]:
        writer.writerow([
            item["severity"], item["label"], item["count"],
            _num(item["area_ha"]), _num(round(item["share"] * 100, 2)),
        ])
    writer.writerow([])

    writer.writerow(["id_контура", "степень", "название", "площадь_га", "дата", "источник"])
    for feature in result["burns"]["features"]:
        properties = feature["properties"]
        writer.writerow([
            properties["id"], properties["severity"], properties["severity_label"],
            _num(properties["area_ha"]), properties["date"], properties["source"],
        ])
    writer.writerow([])

    writer.writerow(["id_термоточки", "дата", "спутник", "прибор", "достоверность", "frp_мвт", "долгота", "широта"])
    for feature in result["hotspots"]["features"]:
        properties = feature["properties"]
        longitude, latitude = feature["geometry"]["coordinates"]
        writer.writerow([
            properties["id"], properties["date"], properties["satellite"],
            properties["instrument"], properties.get("confidence") or "",
            _num(properties.get("frp_mw")), _coord(longitude), _coord(latitude),
        ])
    return buffer.getvalue()


def _num(value: Any) -> str:
    """Числа с запятой в качестве десятичного разделителя — для русской локали Excel."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".replace(".", ",")
    return str(value)


def _coord(value: Any) -> str:
    """Координаты сохраняют шесть знаков: округление до сотых сдвинуло бы точку на километр."""
    if value is None:
        return ""
    return f"{float(value):.6f}".replace(".", ",")


KML_COLORS = {1: "ff4de1ff", 2: "ff1a8cff", 3: "ff2d2dff"}  # aabbggrr: жёлтый, оранжевый, красный


def report_kml(result: dict[str, Any]) -> str:
    """Контуры гари и точки огня в KML: открываются в Google Earth поверх спутниковой подложки.

    Ненадёжные оценки лежат в отдельной папке, по умолчанию скрытой, как и на карте сервиса.
    """
    from xml.sax.saxutils import escape

    def rings(geometry: dict[str, Any]) -> str:
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        out = []
        for polygon in polygons:
            boundaries = []
            for index, ring in enumerate(polygon):
                coords = " ".join(f"{x:.6f},{y:.6f}" for x, y, *_ in ring)
                tag = "outerBoundaryIs" if index == 0 else "innerBoundaryIs"
                boundaries.append(f"<{tag}><LinearRing><coordinates>{coords}</coordinates></LinearRing></{tag}>")
            out.append(f"<Polygon>{''.join(boundaries)}</Polygon>")
        return out[0] if len(out) == 1 else f"<MultiGeometry>{''.join(out)}</MultiGeometry>"

    def burn(feature: dict[str, Any]) -> str:
        p = feature["properties"]
        lines = [f"Степень: {p.get('severity_label')}", f"Площадь: {p.get('area_ha')} га",
                 f"Начало пожара: {p.get('date')}",
                 f"Снимки Sentinel-2: {p.get('pre_image_date', '—')} → {p.get('post_image_date', '—')}",
                 f"Оценка: {'надёжная' if p.get('reliable', True) else 'ненадёжная'}",
                 f"Событие: {p.get('event_id', '—')}", f"Источник: {p.get('source')}"]
        return (f"<Placemark><name>{escape(str(p.get('severity_label')))} · {p.get('area_ha')} га</name>"
                f"<description>{escape(chr(10).join(lines))}</description>"
                f"<styleUrl>#s{p['severity']}</styleUrl>{rings(feature['geometry'])}</Placemark>")

    def hotspot(feature: dict[str, Any]) -> str:
        p = feature["properties"]
        x, y = feature["geometry"]["coordinates"][:2]
        text = (f"{p.get('date')} {p.get('time_utc') or ''} UTC · {p.get('satellite')} · "
                f"достоверность {p.get('confidence')} · FRP {p.get('frp_mw')} МВт")
        return (f"<Placemark><name>{escape(str(p.get('date')))}</name><description>{escape(text)}</description>"
                f"<styleUrl>#fire</styleUrl><Point><coordinates>{x:.6f},{y:.6f}</coordinates></Point></Placemark>")

    def folder(name: str, items: list[str], visible: bool = True) -> str:
        return f"<Folder><name>{escape(name)}</name><visibility>{int(visible)}</visibility>{''.join(items)}</Folder>"

    burns = result["burns"]["features"]
    styles = "".join(
        f'<Style id="s{s}"><LineStyle><color>{c}</color><width>2</width></LineStyle>'
        f'<PolyStyle><color>{"55" + c[2:]}</color></PolyStyle></Style>' for s, c in KML_COLORS.items())
    styles += ('<Style id="fire"><IconStyle><color>ffffe500</color><scale>0.7</scale><Icon><href>'
               'http://maps.google.com/mapfiles/kml/shapes/shaded_dot.png</href></Icon></IconStyle></Style>')
    period = result["summary"]["period"]
    reliable = [burn(f) for f in burns if f["properties"].get("reliable", True)]
    unreliable = [burn(f) for f in burns if not f["properties"].get("reliable", True)]
    body = (folder("Гарь — надёжная оценка", reliable)
            + (folder("Гарь — ненадёжная оценка (длинный разрыв между снимками или тление)", unreliable, False)
               if unreliable else "")
            + folder("Точки огня NASA FIRMS (VIIRS)", [hotspot(f) for f in result["hotspots"]["features"]]))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>Пожары {period['from']} — {period['to']}</name>"
            f"<description>{escape(str((result.get('data_source') or {}).get('description', '')))}</description>"
            f"{styles}{body}</Document></kml>\n")
