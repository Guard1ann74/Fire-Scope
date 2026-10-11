"""Признак «убрано или выжжено» по классам покрова на пожарах Copernicus CLMS.

Перед включением фильтра нужно убедиться, что он не режет настоящую гарь, в том
числе на зелёном покрове (рис, камыш), где красный канал после огня может
посветлеть. Скрипт берёт снимки, сохранённые сервисом при прогоне CLMS
(`validate_bs_clms.py evaluate --tag before`), и для пикселей нашей гари считает:

- «гарь CLMS» — наша гарь внутри контура CLMS: почти наверняка настоящая гарь;
- «вне CLMS» — наша гарь дальше 300 м от контура CLMS: кандидаты в ложную.

Для каждого класса WorldCover — число пикселей, медианы изменения отражения B2,
B3, B4 (после − до) и доля, которую срезал бы фильтр «все три видимых канала
посветлели больше порога». Ничего не скачивается.

    python scripts/measure_harvest_feature.py --results artifacts/validation-clms/results-burn_model-fire-first-before.json \
        --work artifacts/validation-clms --output experiments/validation/harvest-feature-by-landcover.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from shapely.geometry import shape
from shapely.ops import transform
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.config import Settings  # noqa: E402
from service.fire_first import LANDCOVER  # noqa: E402

THRESHOLD = 0.02  # тот же порог, что scenes.harvest_brightening
APPLIES = {"травяные угодья", "пашня", "водно-болотные угодья"}  # scenes.harvest_landcover: 30, 40, 90
INVALID = (0, 1, 3, 8, 9, 10)


def _scene_root(cache: Path, tile: str, event_id: str) -> Path | None:
    """Каталог снимков прогона: метаданные плитки хранят событие и ключ расчёта."""
    found = []
    for meta in cache.glob(f"annotation-scenes/*/metadata/{tile}.json"):
        data = json.loads(meta.read_text(encoding="utf-8"))
        if data.get("event_id") == event_id:
            found.append((meta.stat().st_mtime, meta.parent.parent / "bs"))
    return max(found)[1] if found else None


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    cache = Settings.from_env().satellite_cache_dir
    results = json.loads(args.results.read_text(encoding="utf-8"))
    references = {f["id"]: shape(f["geometry"]) for f in
                  json.loads((args.work / "events.geojson").read_text(encoding="utf-8"))["features"]}
    stats: dict[str, dict[str, dict]] = {"гарь CLMS": {}, "вне CLMS": {}}
    used = []
    for key, result in results.items():
        if "scenes" not in result or key not in references:
            continue
        reference = references[key]
        for scene in result["scenes"]:
            root = _scene_root(cache, scene["tile"], scene["event_id"])
            if root is None:
                continue
            tile = scene["tile"]
            paths = [root / "sentinel2_pre" / f"{tile}_pre.tif", root / "sentinel2_post" / f"{tile}_post.tif",
                     root / "aux" / f"{tile}_aux.tif", root.parent / "predictions" / f"{tile}_severity.tif"]
            if not all(p.is_file() for p in paths):
                continue
            with rasterio.open(paths[0]) as src:
                pre, grid, crs = src.read().astype(np.int32), src.transform, src.crs
            with rasterio.open(paths[1]) as src:
                post = src.read().astype(np.int32)
            with rasterio.open(paths[2]) as src:
                landcover = src.read(3)
            with rasterio.open(paths[3]) as src:
                burned = src.read(1) > 0
            to_tile = Transformer.from_crs(4326, crs, always_xy=True).transform
            ref = transform(to_tile, reference)
            inside = geometry_mask([ref], out_shape=burned.shape, transform=grid, invert=True)
            near = geometry_mask([ref.buffer(300)], out_shape=burned.shape, transform=grid, invert=True)
            valid = ~np.isin(pre[9], INVALID) & ~np.isin(post[9], INVALID)
            change = (post[:3] - pre[:3]) / 10000
            cut = change.min(axis=0) > THRESHOLD
            for group, mask in (("гарь CLMS", burned & inside & valid), ("вне CLMS", burned & ~near & valid)):
                for cls in np.unique(landcover[mask]):
                    pick = mask & (landcover == cls)
                    row = stats[group].setdefault(LANDCOVER.get(int(cls), f"класс {int(cls)}"),
                                                  {"pixels": 0, "cut": 0, "d_b2": [], "d_b3": [], "d_b4": []})
                    row["pixels"] += int(pick.sum())
                    row["cut"] += int((pick & cut).sum())
                    for band, name in ((0, "d_b2"), (1, "d_b3"), (2, "d_b4")):
                        values = change[band][pick]
                        row[name].extend(np.random.default_rng(42).choice(values, min(values.size, 5000),
                                                                          replace=False).tolist())
            used.append(f"{key}:{tile}")
    table = {}
    for group, rows in stats.items():
        table[group] = {}
        for name, row in sorted(rows.items(), key=lambda kv: -kv[1]["pixels"]):
            table[group][name] = {
                "filter_applies": name in APPLIES,
                "pixels": row["pixels"], "ha": round(row["pixels"] * 0.04, 1),
                "share_cut": round(row["cut"] / row["pixels"], 4) if row["pixels"] else None,
                "median_change": {band: round(float(np.median(row[f"d_{band}"])), 4)
                                  for band in ("b2", "b3", "b4") if row[f"d_{band}"]},
            }
    applied = {group: {"ha": round(sum(rows[n]["pixels"] for n in rows if n in APPLIES) * 0.04, 1),
                       "share_cut": round(sum(rows[n]["cut"] for n in rows if n in APPLIES)
                                          / max(sum(rows[n]["pixels"] for n in rows), 1), 4)}
               for group, rows in stats.items()}
    output = {"threshold": THRESHOLD, "applies_to": sorted(APPLIES), "cut_with_landcover_restriction": applied, "rule": "пиксель срезается, если B2, B3 и B4 посветлели больше порога",
              "tiles": len(used), "fires": len({u.split(':')[0] for u in used}), "by_group": table}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
