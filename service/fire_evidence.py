"""Доказательство огня для режима «огонь прежде гари».

Термоточки NASA FIRMS за период запроса проходят три фильтра (низкая
достоверность, статические источники FIRMS, маска постоянных тепловых
источников) и объединяются в события. Модель гари запускается только внутри
буферов этих событий: вопрос «был ли здесь пожар» решают наблюдения огня,
а не падение растительного индекса, которое даёт и убранное поле.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from pyproj import Transformer
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform

API_URL = "https://firms.modaps.eosdis.nasa.gov/api"
REQUEST_TIMEOUT = 60
PLATFORM_NAMES = {"SNPP": "Suomi NPP", "NOAA20": "NOAA-20", "NOAA21": "NOAA-21"}
CONFIDENCE = {"l": "low", "n": "nominal", "h": "high"}
# Архив SP за прошлые даты не меняется; оперативный NRT может дополняться.
NRT_STABLE_AFTER_DAYS = 3


class FireSourceError(RuntimeError):
    """FIRMS не ответил ни на один запрос: доказательство огня получить нельзя."""


@dataclass(frozen=True)
class FirePoint:
    lon: float
    lat: float
    date: date
    time_utc: str | None
    source: str
    platform: str
    confidence: str | None
    frp_mw: float | None
    fire_type: int | None  # только в архиве SP: 0 — растительность, 2 — статический источник
    daynight: str | None = None  # "D" или "N" по данным FIRMS

    @property
    def key(self) -> tuple:
        return (round(self.lat, 4), round(self.lon, 4), self.date, self.time_utc, self.platform)


@dataclass
class FireEvent:
    id: str
    points: list[FirePoint]
    buffer: Polygon  # EPSG:4326, прямоугольник точек с буфером
    epsg: int

    @property
    def first_detected(self) -> date:
        return min(point.date for point in self.points)

    @property
    def last_detected(self) -> date:
        return max(point.date for point in self.points)

    @property
    def max_frp(self) -> float | None:
        values = [point.frp_mw for point in self.points if point.frp_mw is not None]
        return max(values) if values else None

    def evidence(self) -> dict[str, Any]:
        return {"n_points": len(self.points), "first_detected": self.first_detected.isoformat(),
                "last_detected": self.last_detected.isoformat(), "max_frp_mw": self.max_frp}


@dataclass
class Evidence:
    """Итог отбора точек для одной области и периода."""

    points_total: int
    kept: list[FirePoint]
    excluded: dict[str, int]
    sources: list[str]
    failures: list[str] = field(default_factory=list)
    mask_covers_area: bool = False
    persistent: Any = None  # PersistentSources: покрытие маской проверяется по каждому событию


def load_service_config(path: Path) -> dict[str, Any]:
    import yaml

    with Path(path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    if "fire_first" not in config:
        raise ValueError(f"{path}: нет раздела fire_first")
    return config["fire_first"]


def utm_epsg(lon: float, lat: float) -> int:
    zone = min(max(int((lon + 180) // 6) + 1, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


class FirmsArchive:
    """Точки VIIRS из FIRMS Area API: архив SP или оперативный NRT по data_availability."""

    def __init__(self, map_key: str, cache_dir: Path | None, platforms: list[str], window_days: int,
                 opener: Callable = urllib.request.urlopen) -> None:
        self._key = (map_key or "").strip()
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._platforms = list(platforms)
        self._window_days = max(1, min(int(window_days), 5))
        self._open = opener
        self._availability: tuple[float, dict[str, tuple[date, date]]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def _get(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "FireScope/1.0"})
        last: Exception | None = None
        for attempt in range(3):
            try:
                with self._open(request, timeout=REQUEST_TIMEOUT) as response:
                    payload = response.read().decode("utf-8", errors="replace")
                if payload.lstrip().lower().startswith(("invalid", "error")):
                    raise FireSourceError(payload.strip()[:120])
                return payload
            except urllib.error.HTTPError as error:
                if error.code < 500:
                    raise FireSourceError(f"HTTP {error.code}") from error
                last = error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = error
            time.sleep(2 * (attempt + 1))
        raise FireSourceError(f"сеть недоступна ({last})")

    def availability(self) -> dict[str, tuple[date, date]]:
        """Какие даты покрывает каждый набор; обновляется раз в час."""
        if self._availability and time.monotonic() - self._availability[0] < 3600:
            return self._availability[1]
        payload = self._get(f"{API_URL}/data_availability/csv/{self._key}/ALL")
        ranges = {}
        for row in csv.DictReader(io.StringIO(payload)):
            try:
                ranges[row["data_id"]] = (date.fromisoformat(row["min_date"]),
                                          date.fromisoformat(row["max_date"]))
            except (KeyError, ValueError):
                continue
        self._availability = (time.monotonic(), ranges)
        return ranges

    def _sources(self, start: date, end: date) -> list[str]:
        ranges = self.availability()
        chosen = []
        for platform in self._platforms:
            for kind in ("SP", "NRT"):
                name = f"VIIRS_{platform}_{kind}"
                span = ranges.get(name)
                if span and span[0] <= end and start <= span[1]:
                    chosen.append(name)
        return chosen

    def points(self, bounds: tuple[float, float, float, float], start: date,
               end: date) -> tuple[list[FirePoint], list[str], list[str]]:
        """Точки за [start, end]; возвращает точки, использованные наборы и сбои."""
        if not self.enabled:
            raise FireSourceError("Ключ FIRMS_MAP_KEY не задан")
        west, south, east, north = (round(value, 4) for value in bounds)
        today = datetime.now(timezone.utc).date()
        found: dict[tuple, FirePoint] = {}
        used: set[str] = set()
        failures: list[str] = []
        requests = 0
        day = start
        while day <= end:
            span = min(self._window_days, (end - day).days + 1)
            window_end = day + timedelta(days=span - 1)
            for source in self._sources(day, window_end):
                requests += 1
                url = f"{API_URL}/area/csv/{self._key}/{source}/{west},{south},{east},{north}/{span}/{day.isoformat()}"
                stable = source.endswith("_SP") or (today - window_end).days > NRT_STABLE_AFTER_DAYS
                try:
                    payload = self._cached(url, stable)
                except FireSourceError as error:
                    failures.append(f"{source} {day.isoformat()}: {error}")
                    continue
                used.add(source)
                for point in _parse(payload, source):
                    if start <= point.date <= end:
                        # Один и тот же пролёт есть и в SP, и в NRT: оставляем архивную версию.
                        if point.key not in found or source.endswith("_SP"):
                            found[point.key] = point
            day = window_end + timedelta(days=1)
        if requests and len(failures) == requests:
            raise FireSourceError("; ".join(failures[:3]))
        return sorted(found.values(), key=lambda p: (p.date, p.time_utc or "", p.lon)), sorted(used), failures

    def _cached(self, url: str, stable: bool) -> str:
        if self._cache_dir is None or not stable:
            return self._get(url)
        # В имени файла нет ключа: в кеш попадает только хеш URL.
        name = hashlib.sha256(url.encode()).hexdigest()[:32] + ".csv"
        path = self._cache_dir / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
        payload = self._get(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
        return payload


def _parse(payload: str, source: str) -> list[FirePoint]:
    reader = csv.DictReader(io.StringIO(payload))
    if not reader.fieldnames or "latitude" not in reader.fieldnames:
        return []
    platform = PLATFORM_NAMES.get(source.split("_")[1], source)
    points = []
    for row in reader:
        try:
            lon, lat = float(row["longitude"]), float(row["latitude"])
            acquired = date.fromisoformat(str(row["acq_date"]).strip()[:10])
        except (KeyError, TypeError, ValueError):
            continue
        raw_time = str(row.get("acq_time") or "").strip()
        time_utc = f"{raw_time.zfill(4)[:2]}:{raw_time.zfill(4)[2:]}" if raw_time.isdigit() else None
        try:
            frp = round(float(row.get("frp")), 2)
        except (TypeError, ValueError):
            frp = None
        try:
            fire_type = int(row["type"]) if row.get("type") not in (None, "") else None
        except ValueError:
            fire_type = None
        confidence = str(row.get("confidence") or "").strip().lower()
        daynight = str(row.get("daynight") or "").strip().upper() or None
        points.append(FirePoint(lon, lat, acquired, time_utc, source, platform,
                                CONFIDENCE.get(confidence, confidence or None), frp, fire_type, daynight))
    return points


def _display_path(path: Path) -> str:
    """Путь от корня проекта; вне проекта — только имя файла (локальные каталоги наружу не отдаются)."""
    from service.config import PROJECT_ROOT

    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


class PersistentSources:
    """Маска постоянных тепловых источников (scripts/build_persistent_sources.py)."""

    def __init__(self, files: list[Path], radius_m: float) -> None:
        self.radius_m = float(radius_m)
        self.coverage = []
        self._cells: dict[tuple[int, int], list[tuple[float, float]]] = {}
        self.loaded: list[str] = []  # для ответа API: путь от корня проекта, без локальных каталогов
        self.paths: list[Path] = []  # полные пути — для ключа кеша
        for path in files:
            path = Path(path)
            if not path.is_file():
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            for feature in document.get("features", []):
                role = (feature.get("properties") or {}).get("role")
                geometry = shape(feature["geometry"])
                if role == "coverage":
                    self.coverage.append(geometry)
                elif role == "persistent_source":
                    self._add(geometry.x, geometry.y)
            self.paths.append(path)
            self.loaded.append(_display_path(path))

    def _cell(self, lon: float, lat: float) -> tuple[int, int]:
        size = self.radius_m / 111_000.0
        return math.floor(lon / size), math.floor(lat / size)

    def _add(self, lon: float, lat: float) -> None:
        self._cells.setdefault(self._cell(lon, lat), []).append((lon, lat))

    def covers(self, bounds: tuple[float, float, float, float]) -> bool:
        area = box(*bounds)
        return any(region.covers(area) for region in self.coverage)

    def contains(self, lon: float, lat: float) -> bool:
        cx, cy = self._cell(lon, lat)
        scale = math.cos(math.radians(lat))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for slon, slat in self._cells.get((cx + dx, cy + dy), ()):
                    dist = math.hypot((lon - slon) * 111_320 * scale, (lat - slat) * 110_540)
                    if dist <= self.radius_m:
                        return True
        return False


def select_evidence(points: list[FirePoint], config: dict[str, Any], persistent: PersistentSources,
                    bounds: tuple[float, float, float, float], sources: list[str],
                    failures: list[str]) -> Evidence:
    allowed = set(config["evidence"]["min_confidence"])
    excluded_types = set(config["evidence"]["excluded_types"])
    excluded = {"low_confidence": 0, "static_type": 0, "persistent": 0}
    kept = []
    for point in points:
        if point.confidence not in allowed:
            excluded["low_confidence"] += 1
        elif point.fire_type in excluded_types:
            excluded["static_type"] += 1
        elif persistent.contains(point.lon, point.lat):
            excluded["persistent"] += 1
        else:
            kept.append(point)
    return Evidence(len(points), kept, excluded, sources, failures, persistent.covers(bounds), persistent)


def cluster_events(points: list[FirePoint], link_radius_m: float, buffer_m: float,
                   link_days: int | None = None) -> list[FireEvent]:
    """Связные группы точек (одиночная связь по расстоянию и времени) с прямоугольным буфером."""
    if not points:
        return []
    lon0 = sum(p.lon for p in points) / len(points)
    lat0 = sum(p.lat for p in points) / len(points)
    epsg = utm_epsg(lon0, lat0)
    forward = Transformer.from_crs(4326, epsg, always_xy=True).transform
    back = Transformer.from_crs(epsg, 4326, always_xy=True).transform
    xy = [forward(p.lon, p.lat) for p in points]
    parent = list(range(len(points)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid: dict[tuple[int, int], list[int]] = {}
    for index, (x, y) in enumerate(xy):
        grid.setdefault((math.floor(x / link_radius_m), math.floor(y / link_radius_m)), []).append(index)
    for (cx, cy), members in grid.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    for i in members:
                        close_in_time = (link_days is None or
                                         abs((points[i].date - points[j].date).days) <= link_days)
                        if i < j and close_in_time and math.dist(xy[i], xy[j]) <= link_radius_m:
                            parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for index in range(len(points)):
        groups.setdefault(find(index), []).append(index)
    events = []
    for members in groups.values():
        xs = [xy[i][0] for i in members]
        ys = [xy[i][1] for i in members]
        rectangle = box(min(xs) - buffer_m, min(ys) - buffer_m, max(xs) + buffer_m, max(ys) + buffer_m)
        group = [points[i] for i in members]
        first = min(p.date for p in group)
        centre = back((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        key = f"{first.isoformat()}:{centre[0]:.3f}:{centre[1]:.3f}"
        events.append(FireEvent("fire-" + hashlib.sha1(key.encode()).hexdigest()[:10], group,
                                transform(back, rectangle), epsg))
    events.sort(key=lambda event: (-len(event.points), event.first_detected, event.id))
    return events


def persistent_cells(points: list[FirePoint], cell_m: float, min_days: int,
                     min_months: int, require_winter: bool = True) -> list[dict[str, Any]]:
    """Места, где огонь фиксируется снова и снова: кандидаты в постоянные источники.

    Ячейка (~cell_m) считается постоянным источником, если в ней и восьми соседних
    детекции были не меньше чем в min_days разных дней и в min_months разных
    месяцах. Второе условие не даёт пометить как «завод» один многодневный пожар.
    При require_winter нужна ещё хотя бы одна детекция в декабре–феврале:
    факелы и промышленность горят круглый год, а весенние и осенние сельхозпалы
    зимой почти не случаются и в маску не попадают.
    """
    if not points:
        return []
    size = cell_m / 111_000.0
    cells: dict[tuple[int, int], list[FirePoint]] = {}
    for point in points:
        cells.setdefault((math.floor(point.lon / size), math.floor(point.lat / size)), []).append(point)
    found = []
    for (cx, cy), own in cells.items():
        nearby = [p for dx in (-1, 0, 1) for dy in (-1, 0, 1) for p in cells.get((cx + dx, cy + dy), ())]
        days = {p.date for p in nearby}
        months = {(p.date.year, p.date.month) for p in nearby}
        winter = any(p.date.month in (12, 1, 2) for p in nearby)
        if len(days) >= min_days and len(months) >= min_months and (winter or not require_winter):
            found.append({"lon": sum(p.lon for p in own) / len(own), "lat": sum(p.lat for p in own) / len(own),
                          "days": len(days), "months": len(months), "detections": len(nearby),
                          "winter": winter})
    return found


INDUSTRIAL_TAGS = {
    ("landuse", "industrial"): "промзона", ("landuse", "landfill"): "полигон отходов",
    ("man_made", "flare"): "факел", ("man_made", "chimney"): "дымовая труба",
    ("man_made", "works"): "предприятие", ("man_made", "wastewater_plant"): "очистные сооружения",
    ("power", "plant"): "электростанция", ("industrial", "oil"): "нефтепереработка",
    ("industrial", "refinery"): "НПЗ", ("amenity", "waste_disposal"): "площадка отходов",
}


class IndustrialContext:
    """Промышленные объекты OpenStreetMap рядом с точками огня (Overpass API).

    Это пометка, а не фильтр: пожар на промплощадке тоже пожар, но эксперту
    важно знать, что огонь может быть техногенным. Ответы кешируются на диске.
    """

    URL = "https://overpass-api.de/api/interpreter"

    def __init__(self, cache_dir: Path | None, radius_m: float,
                 opener: Callable = urllib.request.urlopen) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self.radius_m = float(radius_m)
        self._open = opener

    def __call__(self, points: list[FirePoint]) -> list[dict[str, Any]] | None:
        """Объекты рядом с точками и сколько точек огня к ним ближе радиуса; None — Overpass недоступен."""
        lon = sum(p.lon for p in points) / len(points)
        lat = sum(p.lat for p in points) / len(points)
        scale = math.cos(math.radians(lat))
        spread = max(math.hypot((p.lon - lon) * 111_320 * scale, (p.lat - lat) * 110_540) for p in points)
        radius = int(spread + self.radius_m)
        key = f"v3_{lon:.4f}_{lat:.4f}_{radius}_{len(points)}"
        path = self._cache_dir / f"{key}.json" if self._cache_dir else None
        if path and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        filters = "".join(f'nwr(around:{radius},{lat:.5f},{lon:.5f})["{k}"="{v}"];' for k, v in INDUSTRIAL_TAGS)
        query = f"[out:json][timeout:25];({filters});out tags center 200;"
        request = urllib.request.Request(self.URL, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": "FireScope/1.0"})
        try:
            with self._open(request, timeout=40) as response:
                elements = json.loads(response.read().decode("utf-8")).get("elements", [])
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        found: dict[tuple[str, str], dict] = {}
        for element in elements:
            tags = element.get("tags", {})
            kind = next((label for (k, v), label in INDUSTRIAL_TAGS.items() if tags.get(k) == v), None)
            where = element.get("center") or element
            if not kind or "lat" not in where or "lon" not in where:
                continue
            # Объект засчитывается, только если он ближе радиуса хотя бы к одной точке огня,
            # а не к центру большого события (у крупного пожара центр далеко от краёв).
            near = {index for index, p in enumerate(points)
                    if math.hypot((p.lon - where["lon"]) * 111_320 * math.cos(math.radians(p.lat)),
                                  (p.lat - where["lat"]) * 110_540) <= self.radius_m}
            if not near:
                continue
            name = tags.get("name") or tags.get("operator") or ""
            entry = found.setdefault((kind, name), {"kind": kind, "name": name, "points": set()})
            entry["points"] |= near
        near_any = set().union(*(entry["points"] for entry in found.values())) if found else set()
        found = [{"kind": e["kind"], "name": e["name"], "points_near": len(e["points"]),
                  "points_total": len(points), "points_near_any": len(near_any)} for e in found.values()]
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
        return found
