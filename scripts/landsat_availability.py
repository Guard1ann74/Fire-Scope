"""Would Landsat 8/9 shorten the Sentinel-2 pairs of real fire-first events?

For every cached event with a pair: Landsat C2 L2 scenes (Planetary Computer) in the same
pre/post search windows; cloud and no-data fractions over the event buffer from QA_PIXEL
with the service thresholds (cloud <= 30 %, no data <= 10 %). Then the pair chosen by the
service rule (cloud rounded to 5 %, then closest to the fire) among Sentinel-2's chosen
scene plus suitable Landsat scenes.
"""
import glob
import json
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom
from rasterio.windows import from_bounds
from shapely.geometry import shape

# python scripts/landsat_availability.py . experiments/validation/landsat-availability.json
# В пути с кириллицей GDAL не открывает сертификаты: GDAL_CURL_CA_BUNDLE — копия cacert.pem по латинскому пути.
ROOT = Path(sys.argv[1])
OUT = Path(sys.argv[2])
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
MAX_CLOUD, MAX_NODATA, STEP = 0.30, 0.10, 0.05
PRE_DAYS, POST_DAYS = 60, 45
TODAY = date(2026, 10, 11)
_token = {}


def post_json(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=120))


def sign(href):
    if "t" not in _token:
        _token["t"] = json.load(urllib.request.urlopen(
            "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2", timeout=60))["token"]
    return f"{href}?{_token['t']}"


def scenes(geometry, start, end):
    body = {"collections": ["landsat-c2-l2"], "intersects": geometry,
            "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z", "limit": 200,
            "query": {"platform": {"in": ["landsat-8", "landsat-9"]}}}
    return post_json(STAC, body)["features"]


def fractions(item, geometry):
    """Cloud and no-data share over the buffer from QA_PIXEL (bits 0 fill, 1–4 cloud/cirrus/shadow)."""
    with rasterio.open(sign(item["assets"]["qa_pixel"]["href"])) as src:
        local = transform_geom("EPSG:4326", src.crs, geometry)
        bounds = shape(local).bounds
        window = from_bounds(*bounds, transform=src.transform)
        qa = src.read(1, window=window, boundless=True, fill_value=1)
        transform = src.window_transform(window)
    inside = geometry_mask([local], out_shape=qa.shape, transform=transform, invert=True)
    total = int(inside.sum()) or 1
    fill = (qa & 1).astype(bool)
    cloud = (qa & 0b11110).astype(bool) & ~fill
    return float((cloud & inside).sum()) / total, float((fill & inside).sum()) / total


def best(candidates, anchor):
    return min(candidates, key=lambda c: (round(c["cloud"] / STEP), abs((c["day"] - anchor).days)))


events = {}
for path in glob.glob(str(ROOT / "artifacts/satellite-cache/fire-first-results/*.json")):
    for event in json.loads(Path(path).read_text(encoding="utf-8"))["prediction"].get("events", []):
        if event.get("pair"):
            events[event["event_id"]] = event

rows = []
for number, event in enumerate(sorted(events.values(), key=lambda e: -e["pair"]["gap_days"]), 1):
    first = date.fromisoformat(event["fire_evidence"]["first_detected"])
    last = date.fromisoformat(event["fire_evidence"]["last_detected"])
    geometry = event["buffer"]
    pair = event["pair"]
    s2 = {"pre": {"day": date.fromisoformat(pair["pre"]), "cloud": pair["cloud_pre"], "sensor": "S2"},
          "post": {"day": date.fromisoformat(pair["post"]), "cloud": pair["cloud_post"], "sensor": "S2"}}
    by_day = {}
    try:
        items = scenes(geometry, first - timedelta(days=PRE_DAYS), min(last + timedelta(days=POST_DAYS), TODAY))
        for item in items:
            day = date.fromisoformat(item["properties"]["datetime"][:10])
            if first <= day <= last:
                continue
            cloud, nodata = fractions(item, geometry)
            known = by_day.get(day)
            if known is None or nodata < known["nodata"]:
                by_day[day] = {"day": day, "cloud": cloud, "nodata": nodata, "sensor": item["properties"]["platform"]}
        error = None
    except Exception as exc:  # сеть: событие записывается с ошибкой, не выдумывается
        error = f"{type(exc).__name__}: {exc}"
    suitable = [c for c in by_day.values() if c["cloud"] <= MAX_CLOUD and c["nodata"] <= MAX_NODATA]
    pre = best([s2["pre"]] + [c for c in suitable if c["day"] < first], first)
    post = best([s2["post"]] + [c for c in suitable if c["day"] > last], last)
    earliest_post = min([c["day"] for c in suitable if c["day"] > last] + [s2["post"]["day"]])
    row = {
        "event_id": event["event_id"], "fire": [first.isoformat(), last.isoformat()],
        "s2_pair": [pair["pre"], pair["post"]], "s2_gap": pair["gap_days"],
        "landsat_scenes": [{"date": c["day"].isoformat(), "sensor": c["sensor"], "cloud": round(c["cloud"], 3),
                            "nodata": round(c["nodata"], 3)} for c in sorted(by_day.values(), key=lambda c: c["day"])],
        "combined_pair": [pre["day"].isoformat(), post["day"].isoformat()],
        "combined_sensors": [pre["sensor"], post["sensor"]],
        "combined_gap": (post["day"] - pre["day"]).days,
        "gap_with_earliest_clear_post": (earliest_post - pre["day"]).days,
        "error": error,
    }
    rows.append(row)
    print(number, row["event_id"], "S2", row["s2_gap"], "-> S2+Landsat", row["combined_gap"], row["combined_sensors"],
          "| самый ранний ясный после:", row["gap_with_earliest_clear_post"], row["error"] or "", flush=True)

long = [r for r in rows if r["s2_gap"] > 15 and not r["error"]]
summary = {
    "events": len(rows), "errors": sum(bool(r["error"]) for r in rows),
    "long_gap_events": len(long),
    "long_gap_became_reliable": sum(r["combined_gap"] <= 15 for r in long),
    "long_gap_shortened": sum(r["combined_gap"] < r["s2_gap"] for r in long),
    "median_gap_s2": float(np.median([r["s2_gap"] for r in rows])),
    "median_gap_combined": float(np.median([r["combined_gap"] for r in rows if not r["error"]])),
    "all_gaps_shortened": sum(r["combined_gap"] < r["s2_gap"] for r in rows if not r["error"]),
}
print(json.dumps(summary, ensure_ascii=False))
OUT.write_text(json.dumps({"method": __doc__.strip(), "checked_at": TODAY.isoformat(), "summary": summary,
                           "events": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
