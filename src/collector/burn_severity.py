"""Materialize resumable Sentinel-2 burn-severity chips and predictions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import box
from shapely.ops import transform



TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"
OPTICAL_BANDS = ("B02", "B03", "B04", "B05", "B06", "B07", "B8A", "B11", "B12")
PIXELS = 512
GSD = 20
TILE_METRES = PIXELS * GSD
PROJECT_DATA = Path(__file__).resolve().parents[2] / "data"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _acquired(item: dict) -> datetime:
    raw = item.get("properties", {}).get("datetime")
    if not raw:
        raise ValueError(f"Scene {item['id']} has no acquisition time")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError(f"Scene {item['id']} has no time zone")
    return value.astimezone(timezone.utc)


def plan_chips(bbox: tuple[float, float, float, float],
               anchor: str = "grid") -> tuple[int, list[dict]]:
    """UTM tiles of 512×512 px at 20 m covering the box.

    `grid` keeps tiles on a fixed global grid (stable chip IDs for collectors);
    `area` starts the grid at the box corner, so a box up to 2 tiles per side
    never needs more than 2×2 tiles.
    """
    if anchor not in {"grid", "area"}:
        raise ValueError("anchor must be grid or area")
    west, south, east, north = bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("--bbox must be west south east north in EPSG:4326")
    center_lon = (west + east) / 2
    zone = int((center_lon + 180) // 6) + 1
    epsg = 32600 + zone if (south + north) / 2 >= 0 else 32700 + zone
    target = f"EPSG:{epsg}"
    polygon = transform(Transformer.from_crs("EPSG:4326", target, always_xy=True).transform,
                        box(*bbox))
    minx, miny, maxx, maxy = polygon.bounds
    chips = []
    if anchor == "area":
        # Угол сетки — на пиксельной сетке 20 м, чтобы снимки совпадали попиксельно.
        origin_x, origin_y = math.floor(minx / GSD) * GSD, math.floor(miny / GSD) * GSD
        columns = range(math.ceil((maxx - origin_x) / TILE_METRES))
        rows = range(math.ceil((maxy - origin_y) / TILE_METRES))
    else:
        origin_x = origin_y = 0
        columns = range(math.floor(minx / TILE_METRES), math.ceil(maxx / TILE_METRES))
        rows = range(math.floor(miny / TILE_METRES), math.ceil(maxy / TILE_METRES))
    for x in columns:
        for y in rows:
            left, bottom = origin_x + x * TILE_METRES, origin_y + y * TILE_METRES
            tile = box(left, bottom, left + TILE_METRES, bottom + TILE_METRES)
            if polygon.intersection(tile).area <= 0:
                continue
            chip_id = (f"BS_ext_{epsg}_{x}_{y}" if anchor == "grid"
                       else f"BS_area_{epsg}_{int(left)}_{int(bottom)}")
            chips.append({"chip_id": chip_id, "epsg": epsg,
                          "x_min": left, "y_min": bottom,
                          "x_max": left + TILE_METRES, "y_max": bottom + TILE_METRES})
    return epsg, chips


def _token(client_id: str | None = None, client_secret: str | None = None) -> str:
    client_id = client_id or os.environ.get("CDSE_CLIENT_ID")
    client_secret = client_secret or os.environ.get("CDSE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("Set CDSE_CLIENT_ID and CDSE_CLIENT_SECRET for Copernicus Process API")
    body = urlencode({"grant_type": "client_credentials", "client_id": client_id,
                      "client_secret": client_secret}).encode()
    request = Request(TOKEN_URL, data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.load(response)["access_token"]


def _evalscript(bands: tuple[str, ...]) -> str:
    names = json.dumps(list(bands))
    output = ", ".join(f"sample.{name}" for name in bands)
    return ("//VERSION=3\n"
            f"function setup() {{ return {{ input: [{{ bands: {names}, units: 'DN' }}], "
            f"output: {{ bands: {len(bands)}, sampleType: 'UINT16' }} }}; }}\n"
            f"function evaluatePixel(sample) {{ return [{output}]; }}")


def _process_request(bounds: dict, acquired: datetime, bands: tuple[str, ...]) -> dict:
    day = acquired.replace(hour=0, minute=0, second=0, microsecond=0)
    when = day.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = (day + timedelta(days=1, seconds=-1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "input": {"bounds": {"bbox": [bounds[k] for k in ("x_min", "y_min", "x_max", "y_max")],
                             "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{bounds['epsg']}"}},
                  "data": [{"type": "sentinel-2-l2a",
                            "dataFilter": {"timeRange": {"from": when, "to": until}},
                            "processing": {"harmonizeValues": True,
                                           "upsampling": "NEAREST" if bands == ("SCL",) else "BILINEAR"}}]},
        "output": {"width": PIXELS, "height": PIXELS,
                   "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
        "evalscript": _evalscript(bands),
    }


def _fetch_raster(token: str, bounds: dict, acquired: datetime,
                  bands: tuple[str, ...]) -> np.ndarray:
    payload = json.dumps(_process_request(bounds, acquired, bands)).encode()
    request = Request(PROCESS_URL, data=payload,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      method="POST")
    with urlopen(request, timeout=120) as response:
        content = response.read()
    with MemoryFile(content) as memory, memory.open() as raster:
        if raster.width != PIXELS or raster.height != PIXELS or raster.count != len(bands):
            raise ValueError("Copernicus returned an unexpected raster shape")
        if raster.crs is None or raster.crs.to_epsg() != bounds["epsg"]:
            raise ValueError("Copernicus returned an unexpected CRS")
        expected = from_origin(bounds["x_min"], bounds["y_max"], GSD, GSD)
        if any(abs(left - right) > 1e-6 for left, right in zip(raster.transform, expected)):
            raise ValueError("Copernicus returned an unexpected pixel grid")
        data = raster.read()
    if data.dtype != np.uint16:
        raise ValueError("Copernicus returned an unexpected raster type")
    return data


def _write_raster(path: Path, data: np.ndarray, bounds: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".tmp.tif")
    with rasterio.open(temporary, "w", driver="GTiff", width=PIXELS, height=PIXELS,
                       count=data.shape[0], dtype=data.dtype,
                       crs=f"EPSG:{bounds['epsg']}",
                       transform=from_origin(bounds["x_min"], bounds["y_max"], GSD, GSD),
                       compress="deflate") as output:
        output.write(data)
    temporary.replace(path)
