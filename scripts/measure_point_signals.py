"""Сигналы надёжности на событиях, подтверждённых Copernicus CLMS: откуда берутся пороги.

Для главного события каждого найденного пожара CLMS-20 (больше всего точек, гарь
совпала с CLMS хотя бы на 10 %) считает:

- площадь гари на одну термоточку FIRMS;
- долю изменений в буфере, срезанную проверкой «убрано или выжжено» (`removed_share`);
- ту же долю в кругах вокруг самих точек (радиус пикселя VIIRS, 375 м) — по маскам,
  сохранённым при расчёте (`filtered/<плитка>_harvest.tif`) и итоговому прогнозу.

Пороги сервиса берутся из этих распределений (настоящие пожары), а не из рисовых
событий; рисовые события из реальных случаев считаются тем же кодом только для
сравнения (`--compare`). Ничего не скачивается.

    python scripts/measure_point_signals.py --results artifacts/validation-clms/results-bs-light-fire-first-after.json \
        --compare experiments/validation/demo-cases-after.json --output experiments/validation/point-signals.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import rasterio
from pyproj import Transformer
from rasterio.features import geometry_mask
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.config import Settings  # noqa: E402

RADIUS_M = 375


def _roots(cache: Path, event_id: str) -> list[Path]:
    """Каталоги расчёта события, где сохранена маска проверки «убрано или выжжено»."""
    found = []
    for meta in cache.glob("annotation-scenes/*/metadata/*.json"):
        data = json.loads(meta.read_text(encoding="utf-8"))
        root = meta.parent.parent
        if data.get("event_id") == event_id and (root / "filtered").is_dir():
            found.append((meta.stat().st_mtime, root, data["chip_id"]))
    if not found:
        return []
    newest = max(found)[1]
    return [(root, chip) for _, root, chip in found if root == newest]


def near_points(cache: Path, event_id: str) -> dict | None:
    pairs = _roots(cache, event_id)
    if not pairs:
        return None
    root = pairs[0][0]
    record = cache / "fire-first-results" / f"{root.name}.json"
    if not record.is_file():
        return None
    hotspots = [h for h in json.loads(record.read_text(encoding="utf-8"))["dataset"]["hotspots"]
                if h["extra"].get("event_id") == event_id]
    burned = removed = 0
    for _, chip in pairs:
        prediction_path = root / "predictions" / f"{chip}_severity.tif"
        harvest_path = root / "filtered" / f"{chip}_harvest.tif"
        if not prediction_path.is_file() or not harvest_path.is_file():
            continue
        with rasterio.open(prediction_path) as src:
            kept, grid, crs = src.read(1) > 0, src.transform, src.crs
        with rasterio.open(harvest_path) as src:
            cut = src.read(1).astype(bool)
        to_tile = Transformer.from_crs(4326, crs, always_xy=True).transform
        circles = [Point(*to_tile(*h["geometry"]["coordinates"])).buffer(RADIUS_M) for h in hotspots]
        near = geometry_mask(circles, out_shape=kept.shape, transform=grid, invert=True)
        burned += int(((kept | cut) & near).sum())
        removed += int((cut & near).sum())
    if not burned:
        return {"near_burned_ha": 0.0, "near_removed_share": None}
    return {"near_burned_ha": round(burned * 0.04, 2), "near_removed_share": round(removed / burned, 4)}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    cache = Settings.from_env().satellite_cache_dir
    rows = []
    for key, result in json.loads(args.results.read_text(encoding="utf-8")).items():
        # Совпадение с CLMS — по всей гари: пометка «ненадёжно» не должна выбрасывать настоящий пожар из выборки.
        if "iou" not in result or (result.get("all_reference_covered") or 0) < 0.10:
            continue
        events = [e for e in result["events"] if e.get("status") == "ok"]
        if not events:
            continue
        main_event = max(events, key=lambda e: e["fire_evidence"]["n_points"])
        rows.append({"fire": key, "month": result["event"]["start"][5:7], "event_id": main_event.get("event_id"),
                     "n_points": main_event["fire_evidence"]["n_points"],
                     "area_per_point_ha": round(main_event["burn_area_ha"] / main_event["fire_evidence"]["n_points"], 2),
                     "removed_share": (main_event.get("harvest_check") or {}).get("removed_share"),
                     **(near_points(cache, main_event.get("event_id")) or {})})
    compare = []
    if args.compare and args.compare.is_file():
        for case, summary in json.loads(args.compare.read_text(encoding="utf-8")).items():
            for e in summary.get("event_details", []):
                if e.get("status") != "ok":
                    continue
                compare.append({"case": case, "event_id": e["event_id"], "n_points": e.get("n_points"),
                                "area_per_point_ha": e.get("area_per_point_ha"),
                                "removed_share": (e.get("harvest_check") or {}).get("removed_share"),
                                **(near_points(cache, e["event_id"]) or {})})

    def spread(name):
        values = sorted(r[name] for r in rows if r.get(name) is not None)
        return {"n": len(values), "min": values[0], "median": values[len(values) // 2], "max": values[-1]} if values else None

    output = {"method": __doc__.strip().split("\n\n")[0], "clms_confirmed_events": rows,
              "distribution": {name: spread(name) for name in ("area_per_point_ha", "removed_share", "near_removed_share")},
              "compare_only_not_for_thresholds": compare}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(output["distribution"], ensure_ascii=False, indent=1))
    for row in rows:
        print("CLMS", row["fire"][-6:], row["month"], row["n_points"], row["area_per_point_ha"], row["removed_share"], row.get("near_removed_share"))
    for row in compare:
        print("сравн.", row["case"][:10], row["event_id"], row["n_points"], row["area_per_point_ha"], row["removed_share"], row.get("near_removed_share"))


if __name__ == "__main__":
    main()
