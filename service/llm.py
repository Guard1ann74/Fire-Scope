"""Краткое объяснение агрегированной статистики через совместимый Chat Completions API."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from datetime import date
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

from service.config import Settings

ENDPOINTS = {
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-flash"),
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
}


class SeverityStats(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    severity: int = Field(ge=1, le=3)
    count: int = Field(ge=0, le=10000000)
    area_ha: float = Field(ge=0, le=1000000000)


class YearStats(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    year: int = Field(ge=2000, le=2100)
    hotspot_count: int = Field(ge=0, le=10000000)
    burn_area_ha: float = Field(ge=0, le=1000000000)


class AnalysisStats(BaseModel):
    """Только агрегаты: никакого произвольного текста, геометрии или снимков."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    period_from: date
    period_to: date
    area_ha: float = Field(ge=0, le=1000000000)
    hotspot_count: int = Field(ge=0, le=10000000)
    burn_count: int = Field(ge=0, le=10000000)
    total_area_ha: float = Field(ge=0, le=1000000000)
    by_severity: list[SeverityStats] = Field(max_length=3)
    frp_total_mw: float = Field(ge=0, le=1000000000)
    peak_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    peak_hotspots: int = Field(ge=0, le=10000000)
    monthly_hotspots: list[int] = Field(default_factory=list, max_length=12)
    yearly: list[YearStats] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def check_period(self) -> "AnalysisStats":
        if self.period_from > self.period_to:
            raise ValueError("Конец периода должен быть позже начала")
        if len({item.severity for item in self.by_severity}) != len(self.by_severity):
            raise ValueError("Степени поражения не должны повторяться")
        if self.monthly_hotspots and (
            len(self.monthly_hotspots) != 12 or any(count < 0 for count in self.monthly_hotspots)
            or sum(self.monthly_hotspots) != self.hotspot_count
        ):
            raise ValueError("Помесячные данные должны содержать 12 значений с общим числом термоточек")
        if self.yearly and (
            len({item.year for item in self.yearly}) != len(self.yearly)
            or sum(item.hotspot_count for item in self.yearly) != self.hotspot_count
        ):
            raise ValueError("Погодовые данные должны соответствовать общему числу термоточек")
        return self


class LLMError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class LLMAnalysis:
    def __init__(self, settings: Settings):
        self.provider = settings.llm_provider
        self.key = settings.llm_api_key
        endpoint = ENDPOINTS.get(self.provider)
        if endpoint is None:
            raise ValueError("FIRES_LLM_PROVIDER: выберите deepseek или openai")
        self.url, default_model = endpoint
        self.model = settings.llm_model or default_model
        self._lock = Lock()
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._cache: dict[str, tuple[float, str]] = {}

    def analyze(self, stats: AnalysisStats, client: str) -> str:
        if not self.key:
            raise LLMError(503, "ИИ-анализ не настроен: задайте ключ API на сервере")

        now = time.monotonic()
        cache_key = stats.model_dump_json()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]
            calls = self._requests[client]
            while calls and calls[0] <= now - 60:
                calls.popleft()
            if len(calls) >= 5:
                raise LLMError(429, "Слишком много запросов к ИИ. Повторите через минуту")
            calls.append(now)
            if len(self._cache) > 100:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": (
                    "Ты объясняешь статистику спутникового мониторинга пожаров по-русски. "
                    "Напиши 3–5 коротких предложений: масштаб и тяжесть гарей, динамика по годам, "
                    "месяцы с наибольшим числом термоточек. Если данные охватывают минимум два года, "
                    "дай осторожный ориентир на будущий сезон: когда по архиву стоит ожидать "
                    "повышенного внимания к возможным очагам. Поясни, на каких числах основан вывод. "
                    "Не обещай конкретного числа пожаров, не называй это точным прогнозом или "
                    "вероятностью: спутниковые детекции не равны числу пожаров. При данных "
                    "за один год не делай прогноза. Не выдумывай погоду и причины."
                )},
                {"role": "user", "content": stats.model_dump_json()},
            ],
            "max_tokens": 350,
            "stream": False,
        }
        request = Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=25) as response:
                result = json.load(response)
            answer = result["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("empty content")
        except HTTPError as error:
            if error.code in (401, 403):
                raise LLMError(502, "Провайдер отклонил ключ ИИ. Проверьте настройки сервера") from error
            if error.code == 429:
                raise LLMError(503, "Лимит провайдера ИИ исчерпан. Повторите позже") from error
            raise LLMError(502, "Провайдер ИИ временно недоступен") from error
        except (URLError, TimeoutError) as error:
            raise LLMError(504, "Нет ответа от провайдера ИИ") from error
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMError(502, "Провайдер ИИ вернул некорректный ответ") from error

        answer = answer.strip()[:1800]
        with self._lock:
            self._cache[cache_key] = (time.monotonic() + 600, answer)
        return answer
