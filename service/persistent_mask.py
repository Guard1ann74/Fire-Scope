"""Маска постоянных тепловых источников (факелы, промышленность): построение и автосборка.

Источник считается постоянным, если огонь в ячейке ~400 м фиксировался во многих разных
днях и месяцах за год, причём хотя бы раз зимой (`configs/service.yaml` →
`persistent_sources`). Готовая маска для Краснодарского края и Адыгеи лежит в
репозитории (`configs/persistent-sources/`); если файла нет и задан ключ FIRMS, сервис
строит её при первом запуске в фоне (`FIRES_BUILD_MASK=0` отключает).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Thread
from typing import Any, Callable

from shapely.geometry import Point, mapping, shape

from service.fire_evidence import FirmsArchive, persistent_cells


def build_mask(region_path: Path, output: Path, config: dict[str, Any], firms_key: str, cache_dir: Path,
               end: date | None = None) -> dict[str, Any]:
    """Построить маску по истории FIRMS за `history_months` и записать GeoJSON; вернуть сводку."""
    params = config["persistent_sources"]
    region = shape(json.loads(Path(region_path).read_text(encoding="utf-8"))["features"][0]["geometry"])
    end = end or datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=round(30.44 * params["history_months"]))
    archive = FirmsArchive(firms_key, cache_dir, config["evidence"]["platforms"], config["evidence"]["window_days"])
    points, sources, failures = archive.points(region.bounds, start, end)
    points = [p for p in points if region.contains(Point(p.lon, p.lat))]
    cells = persistent_cells(points, params["cell_m"], params["min_days"], params["min_months"],
                             params.get("require_winter", True))
    features = [{"type": "Feature", "geometry": mapping(region),
                 "properties": {"role": "coverage", "history_from": start.isoformat(),
                                "history_to": end.isoformat(), "sources": sources,
                                "detections": len(points), "failed_requests": len(failures),
                                "params": params}}]
    features += [{"type": "Feature",
                  "geometry": {"type": "Point", "coordinates": [round(c["lon"], 5), round(c["lat"], 5)]},
                  "properties": {"role": "persistent_source", "days": c["days"], "months": c["months"],
                                 "detections": c["detections"]}} for c in cells]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(output)
    return {"history_from": start.isoformat(), "history_to": end.isoformat(), "detections": len(points),
            "sources": sources, "failed_requests": len(failures), "persistent_sources": len(cells)}


class MaskBootstrap:
    """Фоновая сборка отсутствующей маски при запуске сервиса; состояние — для /api/health."""

    def __init__(self, files: list[Path], region: Path | None, config: dict[str, Any], firms_key: str,
                 cache_dir: Path, on_ready: Callable[[], None]) -> None:
        self.files, self.region, self.config = files, region, config
        self.firms_key, self.cache_dir, self.on_ready = firms_key, cache_dir, on_ready
        self.state: dict[str, Any] = {"status": "ready" if self.missing() == [] else "missing"}
        self._thread: Thread | None = None

    def missing(self) -> list[Path]:
        return [path for path in self.files if not path.is_file()]

    def start(self) -> None:
        missing = self.missing()
        if not missing:
            return
        if not self.firms_key or self.region is None or not self.region.is_file():
            self.state = {"status": "missing",
                          "detail": "Маски нет, а построить её нельзя: нужен FIRMS_MAP_KEY и "
                                    "persistent_sources.region в configs/service.yaml"}
            return
        self.state = {"status": "building", "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._thread = Thread(target=self._run, args=(missing[0],), name="persistent-mask", daemon=True)
        self._thread.start()

    def _run(self, output: Path) -> None:
        try:
            summary = build_mask(self.region, output, self.config, self.firms_key, self.cache_dir)
        except Exception as error:  # сеть или FIRMS: сервис работает без маски и честно об этом пишет
            self.state = {"status": "failed", "detail": str(error)}
            return
        self.state = {"status": "ready", "built": summary}
        self.on_ready()
