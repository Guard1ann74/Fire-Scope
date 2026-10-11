"""Мониторинг: сервис сам опрашивает NASA FIRMS по районам наблюдения и заводит оповещения.

Точки проходят те же фильтры, что и в режиме «огонь прежде гари» (достоверность,
статические и постоянные источники), и группируются в очаги. Новый очаг — новое
оповещение; новые точки рядом с недавним очагом дополняют его, а не плодят
повторы. Состояние хранится в JSON-файле: районы, оповещения и уже учтённые точки.
К оповещению добавляется погода в очаге (Open-Meteo): ветер и класс пожарной опасности.
Промышленные объекты OpenStreetMap здесь не проверяются: десятки запросов к Overpass
за одну проверку упираются в его лимиты. Они проверяются при оценке гари очага.

Скорость ограничена источником: FIRMS публикует точку VIIRS примерно через 3 часа
после пролёта, пролётов над краем около 6 в сутки.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
import uuid

from shapely.geometry import mapping, shape
from shapely.prepared import prep

from service.fire_evidence import FireSourceError, cluster_events, select_evidence


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((a[0] - b[0]) * 111_320 * math.cos(lat), (a[1] - b[1]) * 110_540)


def _point_id(point) -> str:
    lat, lon, day, time_utc, platform = point.key
    return f"{day.isoformat()}|{time_utc}|{platform}|{lat:.4f}|{lon:.4f}"


def _point_dict(point) -> dict[str, Any]:
    return {"lon": round(point.lon, 5), "lat": round(point.lat, 5), "date": point.date.isoformat(),
            "time_utc": point.time_utc, "platform": point.platform, "confidence": point.confidence,
            "frp_mw": point.frp_mw, "daynight": point.daynight, "source": point.source}


class FireMonitor:
    """Районы наблюдения, оповещения и фоновая проверка FIRMS."""

    def __init__(self, path: Path, predictor, config: dict[str, Any], clock=_now, weather=None) -> None:
        self.path = Path(path)
        self.weather = weather  # service.weather.Weather или None
        self.predictor = predictor  # архив FIRMS, маска постоянных источников, OSM и параметры событий
        self.config = config
        self._clock = clock
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self.last_run: str | None = None
        self.next_run: str | None = None
        self._state = self._read()

    # ------------------------------------------------------------ хранилище

    def _read(self) -> dict[str, Any]:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"zones": [], "alerts": [], "seen": {}}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    # --------------------------------------------------------------- районы

    def zones(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(zone) for zone in self._state["zones"]]

    def add_zone(self, name: str, geometry) -> dict[str, Any]:
        from service.fire_evidence import utm_epsg
        from pyproj import Transformer
        from shapely.ops import transform

        limit = float(self.config["max_zone_side_km"]) * 1000
        epsg = utm_epsg(geometry.centroid.x, geometry.centroid.y)
        minx, miny, maxx, maxy = transform(Transformer.from_crs(4326, epsg, always_xy=True).transform,
                                           geometry).bounds
        if maxx - minx > limit or maxy - miny > limit:
            raise ValueError(f"Район {(maxx - minx) / 1000:.0f} × {(maxy - miny) / 1000:.0f} км больше "
                             f"допустимого: до {limit / 1000:.0f} км по стороне")
        zone = {"id": uuid.uuid4().hex[:10], "name": (name or "").strip()[:80] or "Район наблюдения",
                "geometry": mapping(geometry), "created_at": self._clock().isoformat(), "last_check": None}
        with self._lock:
            self._state["zones"].append(zone)
            self._write()
        return dict(zone)

    def remove_zone(self, zone_id: str) -> None:
        with self._lock:
            before = len(self._state["zones"])
            self._state["zones"] = [z for z in self._state["zones"] if z["id"] != zone_id]
            if len(self._state["zones"]) == before:
                raise KeyError(zone_id)
            self._state["alerts"] = [a for a in self._state["alerts"] if a["zone_id"] != zone_id]
            self._state["seen"].pop(zone_id, None)
            self._write()

    # ----------------------------------------------------------- оповещения

    def alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(a) for a in self._state["alerts"]),
                          key=lambda a: (a["updated_at"], a["n_points"]), reverse=True)

    def mark_read(self, alert_id: str | None = None) -> int:
        with self._lock:
            changed = 0
            for alert in self._state["alerts"]:
                if (alert_id is None or alert["id"] == alert_id) and not alert["read"]:
                    alert["read"] = True
                    changed += 1
            if alert_id is not None and not any(a["id"] == alert_id for a in self._state["alerts"]):
                raise KeyError(alert_id)
            self._write()
            return changed

    def summary(self) -> dict[str, Any]:
        alerts = self.alerts()
        return {"enabled": self.predictor.archive.enabled, "running": bool(self._thread and self._thread.is_alive()),
                "poll_minutes": self.config["poll_minutes"], "lookback_hours": self.config["lookback_hours"],
                "last_run": self.last_run, "next_run": self.next_run, "zones": self.zones(),
                "alerts": alerts, "unread": sum(not a["read"] for a in alerts)}

    # ------------------------------------------------------------- проверка

    def check_all(self) -> list[dict[str, Any]]:
        """Проверить все районы; вернуть новые и дополненные оповещения."""
        changed = []
        for zone in self.zones():
            changed.extend(self.check_zone(zone["id"]))
        self.last_run = self._clock().isoformat()
        return changed

    def check_zone(self, zone_id: str) -> list[dict[str, Any]]:
        zone = next((z for z in self.zones() if z["id"] == zone_id), None)
        if zone is None:
            raise KeyError(zone_id)
        now = self._clock()
        area = shape(zone["geometry"])
        start = (now - timedelta(hours=self.config["lookback_hours"])).date()
        record: dict[str, Any] = {"at": now.isoformat(), "ok": True, "error": None,
                                  "points_total": 0, "points_kept": 0, "new_points": 0, "sources": []}
        try:
            points, sources, failures = self.predictor.archive.points(area.bounds, start, now.date())
        except FireSourceError as error:
            record.update(ok=False, error=f"NASA FIRMS недоступен: {error}")
            self._save_check(zone_id, record)
            return []
        inside = prep(area)
        from shapely.geometry import Point

        points = [p for p in points if inside.contains(Point(p.lon, p.lat))
                  and self._observed_at(p) >= now - timedelta(hours=self.config["lookback_hours"])]
        evidence = select_evidence(points, self.predictor.config, self.predictor.persistent,
                                   area.bounds, sources, failures)
        record.update(points_total=len(points), points_kept=len(evidence.kept), sources=sources,
                      excluded=evidence.excluded, failures=failures[:3])
        with self._lock:
            seen = set(self._state["seen"].get(zone_id, []))
        fresh = [p for p in evidence.kept if _point_id(p) not in seen]
        record["new_points"] = len(fresh)
        changed = []
        if fresh:
            events_cfg = self.predictor.config["events"]
            clusters = cluster_events(fresh, events_cfg["link_radius_m"], events_cfg["buffer_m"],
                                      events_cfg.get("link_days"))
            merged: dict[str, dict[str, Any]] = {}
            for cluster in clusters:
                alert = self._merge(zone, cluster.points, now)
                merged[alert["id"]] = alert  # соседние кластеры могли попасть в одно оповещение
            changed = [self._with_weather(alert) for alert in merged.values()]
        with self._lock:
            keep_after = (now - timedelta(hours=self.config["lookback_hours"] + 72)).date()
            ids = seen | {_point_id(p) for p in fresh}
            self._state["seen"][zone_id] = sorted(i for i in ids if date.fromisoformat(i.split("|")[0]) >= keep_after)
            self._write()
        self._save_check(zone_id, record)
        return changed

    def _with_weather(self, alert: dict[str, Any]) -> dict[str, Any]:
        """Погода в центре очага на время последней детекции; сбой Open-Meteo не мешает оповещению."""
        if self.weather is None:
            return alert
        from service.weather import WeatherError

        try:
            weather = self.weather.at(alert["centre"][0], alert["centre"][1],
                                      datetime.fromisoformat(alert["last_seen"]))
        except WeatherError as error:
            weather = {"error": str(error)}
        with self._lock:
            for stored in self._state["alerts"]:
                if stored["id"] == alert["id"]:
                    stored["weather"] = weather
            self._write()
        return {**alert, "weather": weather}

    def _observed_at(self, point) -> datetime:
        hhmm = "".join(ch for ch in (point.time_utc or "") if ch.isdigit()).zfill(4)[:4]
        return datetime(point.date.year, point.date.month, point.date.day, int(hhmm[:2]), int(hhmm[2:]),
                        tzinfo=timezone.utc)

    def _save_check(self, zone_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            for zone in self._state["zones"]:
                if zone["id"] == zone_id:
                    zone["last_check"] = record
            self._write()

    def _merge(self, zone: dict[str, Any], points, now: datetime) -> dict[str, Any]:
        """Новые точки рядом с недавним очагом дополняют его; иначе — новое оповещение."""
        radius = float(self.config["merge_radius_m"])
        horizon = now - timedelta(hours=self.config["merge_hours"])
        coords = [(p.lon, p.lat) for p in points]
        with self._lock:
            target = None
            for alert in self._state["alerts"]:
                if alert["zone_id"] != zone["id"] or datetime.fromisoformat(alert["last_seen"]) < horizon:
                    continue
                if any(_metres((q["lon"], q["lat"]), c) <= radius for q in alert["points"] for c in coords):
                    target = alert
                    break
            added = [_point_dict(p) for p in points]
            if target is None:
                target = {"id": uuid.uuid4().hex[:12], "zone_id": zone["id"], "zone_name": zone["name"],
                          "created_at": now.isoformat(), "points": [], "updates": 0}
                self._state["alerts"].append(target)
            elif target["created_at"] != now.isoformat():  # соседний очаг той же проверки — не «продолжение»
                target["updates"] += 1
            target["points"].extend(added)
            observed = sorted(self._observed_at(p) for p in points)
            target["first_seen"] = min([target.get("first_seen") or observed[0].isoformat(), observed[0].isoformat()])
            target["last_seen"] = max([target.get("last_seen") or observed[-1].isoformat(), observed[-1].isoformat()])
            frp = [q["frp_mw"] for q in target["points"] if q["frp_mw"] is not None]
            target["n_points"] = len(target["points"])
            target["max_frp_mw"] = max(frp) if frp else None
            target["centre"] = [round(sum(q["lon"] for q in target["points"]) / target["n_points"], 5),
                                round(sum(q["lat"] for q in target["points"]) / target["n_points"], 5)]
            target["platforms"] = sorted({q["platform"] for q in target["points"]})
            target["night_only"] = all(q["daynight"] == "N" for q in target["points"])
            target["updated_at"] = now.isoformat()
            target["read"] = False
            self._write()
            return dict(target)

    # ---------------------------------------------------------- фон

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="fire-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        period = float(self.config["poll_minutes"]) * 60
        while not self._stop.is_set():
            try:
                self.check_all()
            except Exception as error:  # фоновая проверка не должна останавливаться навсегда
                self.last_run = f"{self._clock().isoformat()} (ошибка: {error})"
            self.next_run = (self._clock() + timedelta(seconds=period)).isoformat()
            self._stop.wait(period)
