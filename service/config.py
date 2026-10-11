"""Настройки сервиса. Все пути — относительные, значения переопределяются переменными окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
WEB_ROOT = PACKAGE_ROOT / "web"
#: База лежит вне каталога данных моделей: те файлы подменяются целиком.
DEFAULT_DB_PATH = PACKAGE_ROOT / "data" / "service.sqlite3"
# Сервис по умолчанию работает на физической модели гари: ей не нужны веса.
# Ансамбль нейросетей подключается через FIRES_MODEL_CONFIG, когда веса на месте.
DEFAULT_MODEL_CONFIG = PROJECT_ROOT / "configs" / "burn_model.yaml"
DEFAULT_SATELLITE_CACHE = PROJECT_ROOT / "artifacts" / "satellite-cache"
#: Параметры режима «огонь прежде гари»: источники огня, события, подбор снимков.
DEFAULT_SERVICE_CONFIG = PROJECT_ROOT / "configs" / "service.yaml"

#: Подписи степеней поражения. Ключ совпадает с `severity` в API и с `class_id` моделей.
SEVERITY_LABELS: dict[int, str] = {1: "слабая", 2: "средняя", 3: "сильная"}

#: Цвета степеней. Фронтенд получает их из /api/health, чтобы легенда и карта не расходились.
SEVERITY_COLORS: dict[int, str] = {1: "#f2c14e", 2: "#e8712c", 3: "#9e1b1b"}


def use_utf8_output() -> None:
    """Печатать кириллицу в UTF-8.

    Python на Windows берёт кодировку из локали (cp1251), а терминалы — Git Bash,
    Windows Terminal, PowerShell 7 — ожидают UTF-8, и русский текст превращается в кракозябры.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _optional_env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name}: ожидается целое число, получено {raw!r}") from error


def _credential(name: str, filename: str) -> str:
    """Use an explicit environment variable or the project's existing local secrets file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = PROJECT_ROOT / ".secrets" / filename
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, raw = line.removeprefix("export ").partition("=")
        if separator and key.strip() == name:
            return raw.strip().strip("\"'")
    return ""


def _external_apis_enabled(config_path: Path) -> bool:
    """Read the service mode from the same YAML file as the ML selection."""
    import yaml

    with config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    service = config.get("service", {}) if isinstance(config, dict) else {}
    enabled = service.get("use_external_apis", True) if isinstance(service, dict) else True
    if not isinstance(enabled, bool):
        raise ValueError("service.use_external_apis в конфиге модели должен быть true или false")
    return enabled


@dataclass(frozen=True)
class Settings:
    """Параметры запуска.

    Режим `satellite` (по умолчанию) запрашивает реальные снимки и запускает
    модель при обращении к API. Режим `archive` показывает заранее подготовленные
    прогнозы и требует `data_dir`; встроенных демонстрационных данных нет.
    """

    data_dir: Path | None = None
    query_mode: str = "satellite"
    model_config: Path = DEFAULT_MODEL_CONFIG
    service_config: Path = DEFAULT_SERVICE_CONFIG
    satellite_cache_dir: Path = DEFAULT_SATELLITE_CACHE
    cdse_client_id: str = ""
    cdse_client_secret: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    #: База учётных записей и наблюдаемых областей.
    db_path: Path = DEFAULT_DB_PATH
    #: Ключ NASA FIRMS. Пусто — живой слой выключен, остальное работает как прежде.
    firms_map_key: str = ""
    #: Ключ остаётся только на сервере; в HTML и /api/health не выдаётся.
    llm_provider: str = "deepseek"
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    #: Глубина запроса к FIRMS в сутках, предел самого API — 5.
    live_day_range: int = 2
    #: Максимальное число вершин в пользовательском полигоне.
    max_vertices: int = 2000
    #: Максимальный период запроса в днях.
    max_period_days: int = 3660
    #: Кука сессии помечается Secure только под HTTPS: на localhost это сломало бы вход.
    secure_cookies: bool = False
    #: Фоновый опрос FIRMS по районам наблюдения (выключен в тестах: там нет сети).
    monitor_background: bool = False
    #: Построить отсутствующую маску постоянных источников при запуске (выключено в тестах: там нет сети).
    build_mask: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        model_config = _env_path("FIRES_MODEL_CONFIG", DEFAULT_MODEL_CONFIG)
        configured_mode = "satellite" if _external_apis_enabled(model_config) else "archive"
        provider = os.environ.get("FIRES_LLM_PROVIDER", "deepseek").strip().lower() or "deepseek"
        if provider not in {"deepseek", "openai"}:
            raise ValueError("FIRES_LLM_PROVIDER: выберите deepseek или openai")
        provider_key = "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
        return cls(
            data_dir=_optional_env_path("FIRES_DATA_DIR"),
            query_mode=os.environ.get("FIRES_QUERY_MODE", "").strip().lower() or configured_mode,
            model_config=model_config,
            service_config=_env_path("FIRES_SERVICE_CONFIG", DEFAULT_SERVICE_CONFIG),
            satellite_cache_dir=_env_path("FIRES_SATELLITE_CACHE", DEFAULT_SATELLITE_CACHE),
            cdse_client_id=_credential("CDSE_CLIENT_ID", "cdse.env"),
            cdse_client_secret=_credential("CDSE_CLIENT_SECRET", "cdse.env"),
            host=os.environ.get("FIRES_HOST", "").strip() or "127.0.0.1",
            port=_env_int("FIRES_PORT", 8080),
            db_path=_env_path("FIRES_DB", DEFAULT_DB_PATH),
            firms_map_key=_credential("FIRMS_MAP_KEY", "firms.env"),
            llm_provider=provider,
            llm_api_key=_credential("FIRES_LLM_API_KEY", "llm.env") or
                        _credential(provider_key, "llm.env"),
            llm_model=os.environ.get("FIRES_LLM_MODEL", "").strip(),
            live_day_range=max(1, min(_env_int("FIRES_LIVE_DAYS", 2), 5)),
            secure_cookies=os.environ.get("FIRES_SECURE_COOKIES", "").strip().lower()
            in ("1", "true", "yes"),
            monitor_background=os.environ.get("FIRES_MONITOR", "1").strip().lower() not in ("0", "false", "no"),
            build_mask=os.environ.get("FIRES_BUILD_MASK", "1").strip().lower() not in ("0", "false", "no"),
        )

    @property
    def live_enabled(self) -> bool:
        return bool(self.firms_map_key)
