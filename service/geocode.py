"""Lightweight place search through Photon's public demo API.

Requests are debounced in the browser, serialized here, and cached to keep
traffic modest. The provider can be changed without touching the map UI.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any


PHOTON_URL = "https://photon.komoot.io/api"
CACHE_SECONDS = 3600
REQUEST_INTERVAL = 1.0
USER_AGENT = "FireScope/1.0 (+https://github.com/Guard1ann74/Fire-Scope)"
LAYERS = ("city", "state", "country", "locality", "county", "district")
TYPE_LABELS = {"city": "Город", "country": "Страна", "state": "Регион",
               "county": "Округ", "district": "Район", "locality": "Населённый пункт"}


class GeocodingError(RuntimeError):
    """The external place-search service is temporarily unavailable."""


class PhotonSearch:
    def __init__(self) -> None:
        self._cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self._lock = threading.Lock()
        self._last_request = 0.0

    def search(self, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        key = query.casefold()
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                self._cache.move_to_end(key)
                return cached[1]

            delay = REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            params = [("q", query), ("limit", "6"), *(("layer", layer) for layer in LAYERS)]
            url = f"{PHOTON_URL}?{urllib.parse.urlencode(params)}"
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.load(response)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                raise GeocodingError("Поиск мест сейчас недоступен. Повторите запрос позже.") from error

            if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
                raise GeocodingError("Сервис поиска вернул неожиданный ответ. Повторите запрос позже.")
            results = []
            for feature in payload["features"]:
                if not isinstance(feature, dict):
                    continue
                place = _place(feature)
                if place:
                    results.append(place)
            self._cache[key] = (time.monotonic(), results)
            if len(self._cache) > 200:
                self._cache.popitem(last=False)
            return results


def _place(feature: dict[str, Any]) -> dict[str, Any] | None:
    properties = feature.get("properties") or {}
    coordinates = (feature.get("geometry") or {}).get("coordinates") or []
    name = properties.get("name")
    if not isinstance(name, str) or not name.strip() or len(coordinates) < 2:
        return None
    try:
        lon, lat = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None
    if not (-180 <= lon <= 180 and -85 <= lat <= 85):
        return None
    parts = []
    for field in ("city", "county", "state", "country"):
        value = properties.get(field)
        if isinstance(value, str) and value and value != name and value not in parts:
            parts.append(value)
    extent = properties.get("extent")
    bbox = None
    if isinstance(extent, list) and len(extent) == 4:
        try:
            west, north, east, south = map(float, extent)
            if -180 <= west < east <= 180 and -85 <= south < north <= 85:
                bbox = [west, south, east, north]
        except (TypeError, ValueError):
            pass
    detail = ", ".join(parts) or TYPE_LABELS.get(str(properties.get("type")), "Место на карте")
    return {"name": name.strip(), "detail": detail, "lat": lat, "lon": lon,
            "bbox": bbox, "type": str(properties.get("type") or "place")}
