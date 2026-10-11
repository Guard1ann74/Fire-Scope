"""Choose dated, low-cloud Sentinel-2 scenes covering an event area."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from shapely.geometry import box, shape


def sentinel2_candidates(items: list[dict], bbox: tuple[float, float, float, float],
                         event_from: date, event_to: date, max_cloud: float = 30.0,
                         window_days: int = 30) -> tuple[list[dict], list[dict]]:
    """Scenes before and after the event, nearest to it first.

    `eo:cloud_cover` describes the whole ~110 km granule, not the event area, so the
    order is only a starting point: callers that can read the area should check it.
    """
    if event_from > event_to or not 0 <= max_cloud <= 100 or window_days < 1:
        raise ValueError("Invalid event dates, cloud limit, or search window")
    area = box(*bbox)
    if not area.is_valid or area.is_empty or area.area == 0:
        raise ValueError("Invalid event bounding box")
    candidates = []
    for item in items:
        if item.get("collection") != "sentinel-2-l2a" or not item.get("geometry"):
            continue
        properties = item.get("properties", {})
        raw_time = properties.get("datetime")
        if not raw_time:
            continue
        acquired = datetime.fromisoformat(raw_time.replace("Z", "+00:00")).date()
        cloud = properties.get("eo:cloud_cover")
        if cloud is None or float(cloud) > max_cloud:
            continue
        if shape(item["geometry"]).intersection(area).area / area.area < 0.95:
            continue
        candidates.append((item, acquired, float(cloud)))

    before = [(item, (event_from - acquired).days, cloud)
              for item, acquired, cloud in candidates
              if event_from - timedelta(days=window_days) <= acquired < event_from]
    after = [(item, (acquired - event_to).days, cloud)
             for item, acquired, cloud in candidates
             if event_to < acquired <= event_to + timedelta(days=window_days)]
    order = lambda row: (row[1], row[2], row[0]["id"])
    return ([row[0] for row in sorted(before, key=order)],
            [row[0] for row in sorted(after, key=order)])


def select_sentinel2_pair(items: list[dict], bbox: tuple[float, float, float, float],
                          event_from: date, event_to: date, max_cloud: float = 30.0,
                          window_days: int = 30) -> tuple[dict, dict]:
    before, after = sentinel2_candidates(items, bbox, event_from, event_to,
                                         max_cloud, window_days)
    if not before or not after:
        raise ValueError("No low-cloud Sentinel-2 pair covers the area around the event")
    return before[0], after[0]
