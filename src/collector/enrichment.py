"""Add Sentinel-1 and public terrain/land-cover layers to collected BS chips."""

from __future__ import annotations

import math
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from shapely.geometry import box
from shapely.ops import transform

from src.collector.burn_severity import GSD, PIXELS


def _geographic_polygon(bounds: dict):
    project = Transformer.from_crs(f"EPSG:{bounds['epsg']}", "EPSG:4326", always_xy=True).transform
    return transform(project, box(bounds["x_min"], bounds["y_min"],
                                  bounds["x_max"], bounds["y_max"]))


def _fetch_file(url: str, cache: Path, attempts: int = 5) -> Path:
    """Download a large static tile once; resume after a dropped connection."""
    if cache.is_file():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_name(cache.name + ".tmp")
    for attempt in range(attempts):
        offset = temporary.stat().st_size if temporary.is_file() else 0
        request = Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
        try:
            with urlopen(request, timeout=120) as response:
                # 206 — сервер продолжил с места обрыва; 200 — отдал файл целиком.
                mode = "ab" if offset and response.status == 206 else "wb"
                with temporary.open(mode) as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
            break
        except HTTPError as error:
            if error.code == 416:  # файл уже докачан целиком
                break
            raise
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
    temporary.replace(cache)
    return cache


def _tile_range(west: float, south: float, east: float, north: float, step: int):
    for latitude in range(math.floor(south / step) * step, math.ceil(north / step) * step, step):
        for longitude in range(math.floor(west / step) * step, math.ceil(east / step) * step, step):
            yield latitude, longitude


def _coordinate_label(latitude: int, longitude: int, suffix: str = "") -> str:
    return (f"{'N' if latitude >= 0 else 'S'}{abs(latitude):02d}{suffix}"
            f"_{'E' if longitude >= 0 else 'W'}{abs(longitude):03d}{suffix}")


def _reproject_tiles(bounds: dict, cache: Path, product: str,
                     pixels: int = PIXELS, gsd: int = GSD) -> tuple[np.ndarray, list[str]]:
    west, south, east, north = _geographic_polygon(bounds).bounds
    destination = np.full((pixels, pixels), np.nan if product == "dem" else 0,
                          dtype=np.float32 if product == "dem" else np.uint8)
    urls = []
    step = 1 if product == "dem" else 3
    for latitude, longitude in _tile_range(west, south, east, north, step):
        if product == "dem":
            label = f"Copernicus_DSM_COG_10_{_coordinate_label(latitude, longitude, '_00')}_DEM"
            url = f"https://copernicus-dem-30m.s3.amazonaws.com/{label}/{label}.tif"
            method = Resampling.bilinear
        else:
            label = (f"ESA_WorldCover_10m_2021_v200_"
                     f"{_coordinate_label(latitude, longitude).replace('_', '')}_Map.tif")
            url = f"https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/{label}"
            method = Resampling.nearest
        source_path = _fetch_file(url, cache / product / label)
        with rasterio.open(source_path) as source:
            reproject(source=rasterio.band(source, 1), destination=destination,
                      src_transform=source.transform, src_crs=source.crs,
                      src_nodata=source.nodata, dst_transform=from_origin(
                          bounds["x_min"], bounds["y_max"], gsd, gsd),
                      dst_crs=f"EPSG:{bounds['epsg']}",
                      dst_nodata=np.nan if product == "dem" else 0,
                      init_dest_nodata=False, resampling=method)
        urls.append(url)
    if product == "dem" and not np.isfinite(destination).all():
        raise ValueError("Copernicus DEM does not cover the complete chip")
    if product == "landcover" and np.count_nonzero(destination) < 0.95 * pixels * pixels:
        raise ValueError("ESA WorldCover does not cover the complete chip")
    return destination, urls


def _aux(bounds: dict, cache: Path) -> tuple[np.ndarray, dict]:
    dem, dem_urls = _reproject_tiles(bounds, cache, "dem")
    landcover, landcover_urls = _reproject_tiles(bounds, cache, "landcover")
    north_slope, east_slope = np.gradient(dem, GSD)
    slope = np.degrees(np.arctan(np.hypot(north_slope, east_slope)))
    bands = np.stack([np.rint(dem), np.rint(slope), landcover]).astype(np.int16)
    return bands, {"dem": dem_urls, "landcover": landcover_urls,
                   "aux_bands": ["elevation_m", "slope_degrees", "worldcover_2021_v200"]}
