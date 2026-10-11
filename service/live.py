"""Живой слой термоточек NASA FIRMS.

FIRMS отдаёт детекции активного горения VIIRS с задержкой около трёх часов —
это единственный источник, который в самом деле работает «сейчас». Контуры гарей
так получить нельзя: Sentinel-2 возвращается на точку раз в пять дней, поэтому
второй этап мониторинга остаётся ретроспективным.

Слой включается сам, когда задан FIRMS_MAP_KEY, и молчит без него. Любая ошибка
сети или разбора не роняет запрос: сервис честно сообщает, что живые данные
недоступны, и продолжает работать на подготовленных файлах.

Ключ бесплатный: https://firms.modaps.eosdis.nasa.gov/api/area/
"""

from __future__ import annotations

import csv
import io
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
#: Продукты ближайшего реального времени. Те же спутники, что и в подготовленных данных.
SOURCES = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")
#: Ограничение API: за один запрос не больше пяти суток.
MAX_DAY_RANGE = 5
REQUEST_TIMEOUT = 20
#: Ответ кэшируется: у ключа лимит 5000 обращений за 10 минут, а данные обновляются нечасто.
CACHE_SECONDS = 300

#: Коды платформ в колонке satellite.
PLATFORMS = {"N": "NOAA-20", "N20": "NOAA-20", "N21": "NOAA-21", "1": "Suomi NPP", "NPP": "Suomi NPP"}
#: У VIIRS достоверность категориальная, одной буквой.
CONFIDENCE = {"l": "low", "n": "nominal", "h": "high"}


class LiveDataError(RuntimeError):
    """Живой источник недоступен. Не прерывает запрос, попадает в поле notice."""


@dataclass(frozen=True)
class LiveResult:
    """Результат обращения к FIRMS вместе с описанием состояния источника."""

    features: list[dict[str, Any]]
    fetched_at: str
    sources: tuple[str, ...]
    day_range: int
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "count": len(self.features),
            "fetched_at": self.fetched_at,
            "sources": list(self.sources),
            "day_range": self.day_range,
            "latency_note": "Задержка NASA FIRMS около 3 часов",
            "error": self.error,
        }


class FirmsClient:
    """Клиент FIRMS с кэшем по ключу «область + глубина запроса»."""

    def __init__(self, map_key: str | None, cache_seconds: int = CACHE_SECONDS,
                 disabled_reason: str | None = None) -> None:
        self._map_key = (map_key or "").strip()
        self._cache_seconds = cache_seconds
        self.disabled_reason = disabled_reason or "Ключ FIRMS_MAP_KEY не задан"
        self._cache: dict[tuple, tuple[float, LiveResult]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._map_key)

    def fetch(self, bounds: tuple[float, float, float, float], day_range: int = 1) -> LiveResult:
        day_range = max(1, min(int(day_range), MAX_DAY_RANGE))
        if not self.enabled:
            return LiveResult([], _now(), SOURCES, day_range,
                              error=self.disabled_reason)

        west, south, east, north = (round(value, 4) for value in bounds)
        cache_key = (west, south, east, north, day_range)
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self._cache_seconds:
            return cached[1]

        features: list[dict[str, Any]] = []
        failures: list[str] = []
        for source in SOURCES:
            url = f"{BASE_URL}/{self._map_key}/{source}/{west},{south},{east},{north}/{day_range}"
            try:
                features.extend(self._read(url, source))
            except LiveDataError as error:
                failures.append(f"{source}: {error}")

        if failures and not features:
            result = LiveResult([], _now(), SOURCES, day_range, error="; ".join(failures))
        else:
            features.sort(key=lambda item: item["properties"]["date"], reverse=True)
            result = LiveResult(features, _now(), SOURCES, day_range,
                                error="; ".join(failures) if failures else None)
        self._cache[cache_key] = (time.monotonic(), result)
        return result

    def _read(self, url: str, source: str) -> list[dict[str, Any]]:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "FireScope/1.0"})
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            hint = " — проверьте MAP_KEY" if error.code in (401, 403) else ""
            raise LiveDataError(f"HTTP {error.code}{hint}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LiveDataError(f"сеть недоступна ({error})") from error

        # Ответ из одного заголовка — это «детекций нет», а не сбой.
        if payload.lstrip().lower().startswith(("invalid", "error")):
            raise LiveDataError(payload.strip()[:120])
        return _parse_csv(payload, source)


def _parse_csv(payload: str, source: str) -> list[dict[str, Any]]:
    """Разбор по именам колонок: порядок у FIRMS может отличаться между продуктами."""
    reader = csv.DictReader(io.StringIO(payload))
    if not reader.fieldnames or "latitude" not in reader.fieldnames:
        return []
    features = []
    for index, row in enumerate(reader, start=1):
        try:
            longitude = float(row["longitude"])
            latitude = float(row["latitude"])
        except (KeyError, TypeError, ValueError):
            continue
        acquired = _parse_date(row.get("acq_date"))
        if acquired is None:
            continue
        features.append({
            "type": "Feature",
            "id": f"FIRMS-{source}-{index:05d}",
            "geometry": {"type": "Point", "coordinates": [round(longitude, 6), round(latitude, 6)]},
            "properties": {
                "id": f"FIRMS-{source}-{index:05d}",
                "date": acquired.isoformat(),
                "time_utc": _parse_time(row.get("acq_time")),
                "satellite": PLATFORMS.get(str(row.get("satellite", "")).strip(),
                                           str(row.get("satellite") or "не указан")),
                "instrument": str(row.get("instrument") or "VIIRS"),
                "confidence": CONFIDENCE.get(str(row.get("confidence", "")).strip().lower(),
                                             str(row.get("confidence") or "") or None),
                "frp_mw": _parse_float(row.get("frp")),
                "brightness_k": _parse_float(row.get("bright_ti4") or row.get("brightness")),
                "daynight": "день" if str(row.get("daynight", "")).strip().upper() == "D" else "ночь",
                "live": True,
                "source": source,
            },
        })
    return features


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (AttributeError, TypeError, ValueError):
        return None


def _parse_time(value: Any) -> str | None:
    """acq_time приходит как HHMM без ведущего нуля."""
    raw = str(value or "").strip()
    if not raw.isdigit():
        return None
    raw = raw.zfill(4)
    return f"{raw[:2]}:{raw[2:]}"


def _parse_float(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
