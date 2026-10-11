"""Find Copernicus scenes in resumable spatial and temporal batches."""

from __future__ import annotations

from datetime import date
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen



STAC_SEARCH_URL = "https://stac.dataspace.copernicus.eu/v1/search"
COLLECTIONS = ("sentinel-2-l2a", "sentinel-1-grd")


def _request_page(url: str, query: dict, attempts: int = 3) -> dict:
    request = Request(
        url,
        data=json.dumps(query).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/geo+json"},
        method="POST",
    )
    # Страница из сотни сцен с полными метаданными весит мегабайты: на медленном
    # канале нужен запас по времени и повтор, иначе поиск обрывается по таймауту.
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=120) as response:
                catalogue = json.load(response)
            break
        except HTTPError as exc:
            if exc.code < 500 or attempt == attempts - 1:
                raise RuntimeError(f"Copernicus catalogue request failed: {exc}") from exc
        except (URLError, OSError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f"Copernicus catalogue request failed: {exc}") from exc
        time.sleep(5 * (attempt + 1))
    if catalogue.get("type") != "FeatureCollection" or not isinstance(catalogue.get("features"), list):
        raise ValueError("Unexpected response from Copernicus catalogue")
    return catalogue


def search_scenes(aoi: dict, start: date, end: date, collection: str, limit: int,
                  max_pages: int = 20) -> dict:
    if start > end:
        raise ValueError("Start date must not be later than end date")
    if collection not in COLLECTIONS:
        raise ValueError(f"Unknown collection: {collection}")
    if limit < 1 or limit > 100:
        raise ValueError("Limit must be between 1 and 100")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    query = {
        "collections": [collection],
        "intersects": aoi,
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": limit,
    }
    items: dict[str, dict] = {}
    url, page_query = STAC_SEARCH_URL, query
    seen_pages: set[str] = set()
    for _ in range(max_pages):
        page_key = json.dumps([url, page_query], sort_keys=True)
        if page_key in seen_pages:
            raise RuntimeError("Copernicus catalogue returned a repeated page")
        seen_pages.add(page_key)
        catalogue = _request_page(url, page_query)
        for item in catalogue["features"]:
            items[item["id"]] = item
        following = next((link for link in catalogue.get("links", []) if link.get("rel") == "next"), None)
        if following is None:
            return {"source": STAC_SEARCH_URL, "query": query, "items": list(items.values()),
                    "pages": len(seen_pages)}
        if following.get("method", "GET").upper() != "POST" or not isinstance(following.get("body"), dict):
            raise RuntimeError("Unsupported Copernicus catalogue pagination link")
        url, page_query = following["href"], following["body"]
        if not url.startswith(STAC_SEARCH_URL):
            raise RuntimeError("Unexpected pagination URL from Copernicus catalogue")
    raise RuntimeError(f"A batch needs more than {max_pages} catalogue pages; increase --max-pages")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
