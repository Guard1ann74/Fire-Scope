"""Погода в очаге: Open-Meteo, бесплатно и без ключа.

Для времени огня даются температура, влажность, ветер (откуда дует и куда сносит
огонь) и комплексный показатель пожарной опасности Нестерова с классом I–V, как
в шкале лесной охраны. Это погода модельной ячейки (2–25 км), а не измерение на
месте: показатель Нестерова лесная охрана считает по метеостанции, здесь — по
модели, поэтому класс ориентировочный.

Последние ~3 месяца берутся из оперативных моделей погоды Open-Meteo, более
ранние даты — из реанализа ERA5. Ответы кешируются: прошедшие дни на диске,
текущие сутки — в памяти на полчаса.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = ("temperature_2m", "relative_humidity_2m", "dew_point_2m", "wind_speed_10m",
          "wind_direction_10m", "wind_gusts_10m", "precipitation")
#: Оперативный API хранит прошлые дни примерно на 3 месяца назад; дальше — ERA5.
FORECAST_DAYS_BACK = 85
#: За сколько дней до огня накапливается показатель Нестерова.
NESTEROV_DAYS = 30
#: Осадки за сутки больше этого обнуляют показатель.
RAIN_RESET_MM = 3.0
#: Верхние границы классов пожарной опасности по Нестерову (I–IV); выше — V.
DANGER_LIMITS = (300, 1000, 4000, 10000)
COMPASS = ("С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ")
LIVE_TTL_S = 1800


class WeatherError(RuntimeError):
    """Open-Meteo недоступен или ответил не так, как ожидалось."""


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "FireScope/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise WeatherError(f"Open-Meteo ответил {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise WeatherError(f"Open-Meteo недоступен: {error}") from error


def compass(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    return COMPASS[int(((degrees % 360) + 22.5) // 45) % 8]


def danger_class(index: float) -> int:
    return next((number for number, limit in enumerate(DANGER_LIMITS, 1) if index <= limit), 5)


class Weather:
    def __init__(self, cache_dir: Path | None, fetch: Callable[[str], dict] = _fetch_json,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._fetch = fetch
        self._today = today
        self._live: dict[str, tuple[float, dict]] = {}

    def at(self, lon: float, lat: float, when: datetime) -> dict[str, Any]:
        """Погода в ячейке на час огня и показатель Нестерова на этот момент."""
        when = when.astimezone(timezone.utc)
        series, source = self._series(round(lon, 1), round(lat, 1),
                                      when.date() - timedelta(days=NESTEROV_DAYS), when.date())
        offset = timedelta(seconds=int(series.get("utc_offset_seconds") or 0))
        local = (when + offset).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        hourly = series["hourly"]
        times = [datetime.fromisoformat(value) for value in hourly["time"]]
        upto = [i for i, moment in enumerate(times) if moment <= local]
        if not upto:
            raise WeatherError("Open-Meteo не вернул данных на время огня")
        now = upto[-1]

        def value(name: str, index: int) -> float | None:
            data = hourly.get(name) or []
            return data[index] if index < len(data) else None

        nesterov, dry_days = self._nesterov(hourly, times, now)
        rain = [value("precipitation", i) or 0.0 for i in upto[-24:]]
        wind_from = value("wind_direction_10m", now)
        return {
            "at": times[now].isoformat(timespec="minutes"), "utc_offset_hours": offset.total_seconds() / 3600,
            "temperature_c": value("temperature_2m", now), "humidity_pct": value("relative_humidity_2m", now),
            "wind_speed_ms": value("wind_speed_10m", now), "wind_gusts_ms": value("wind_gusts_10m", now),
            "wind_from_deg": wind_from, "wind_from": compass(wind_from),
            # Огонь сносит по ветру — в сторону, противоположную той, откуда дует.
            "spread_to": compass(None if wind_from is None else wind_from + 180),
            "precip_24h_mm": round(sum(rain), 1),
            "nesterov": round(nesterov), "danger_class": danger_class(nesterov), "dry_days": dry_days,
            "source": source, "cell": [round(lon, 1), round(lat, 1)],
        }

    @staticmethod
    def _nesterov(hourly: dict, times: list[datetime], now: int) -> tuple[float, int]:
        """Σ t·(t − td) в 13 ч по дням без дождя; осадки > 3 мм за сутки обнуляют сумму."""
        index, dry_days = 0.0, 0
        days = sorted({moment.date() for moment in times[:now + 1]})
        for day in days:
            hours = [i for i in range(now + 1) if times[i].date() == day]
            rain = sum(hourly["precipitation"][i] or 0.0 for i in hours)
            if rain > RAIN_RESET_MM:
                index, dry_days = 0.0, 0
                continue
            noon = next((i for i in hours if times[i].hour == 13), None)
            if noon is None:  # день огня до 13 ч: показатель ещё прежний
                continue
            t, td = hourly["temperature_2m"][noon], hourly["dew_point_2m"][noon]
            if t is None or td is None:
                continue
            dry_days += 1
            if t > 0:
                index += t * (t - td)
        return index, dry_days

    def _series(self, lon: float, lat: float, start: date, end: date) -> tuple[dict, str]:
        archive = start < self._today() - timedelta(days=FORECAST_DAYS_BACK)
        source = "Open-Meteo, реанализ ERA5" if archive else "Open-Meteo, оперативные модели погоды"
        key = f"{lon:.1f}_{lat:.1f}_{start.isoformat()}_{end.isoformat()}"
        final = end < self._today() - timedelta(days=1)
        path = self.cache_dir / f"{key}.json" if self.cache_dir else None
        if final and path and path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), source
        cached = self._live.get(key)
        if not final and cached and time.monotonic() - cached[0] < LIVE_TTL_S:
            return cached[1], source
        query = urllib.parse.urlencode({
            "latitude": f"{lat:.2f}", "longitude": f"{lon:.2f}", "hourly": ",".join(HOURLY),
            "wind_speed_unit": "ms", "timezone": "auto",
            "start_date": start.isoformat(), "end_date": end.isoformat()})
        data = self._fetch(f"{ARCHIVE_URL if archive else FORECAST_URL}?{query}")
        if data.get("error") or "hourly" not in data:
            raise WeatherError(f"Open-Meteo: {data.get('reason') or 'нет почасовых данных'}")
        if final and path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        else:
            self._live[key] = (time.monotonic(), data)
        return data, source


def observed_at(point) -> datetime:
    """Время детекции FIRMS (дата + время UTC в виде «HHMM» или «HH:MM»)."""
    hhmm = "".join(ch for ch in (point.time_utc or "") if ch.isdigit()).zfill(4)[:4]
    return datetime(point.date.year, point.date.month, point.date.day, int(hhmm[:2]), int(hhmm[2:]),
                    tzinfo=timezone.utc)
