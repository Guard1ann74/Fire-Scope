"""REST API и раздача интерфейса.

Выдача данных строится вокруг одной операции — `run_query`, поэтому карта,
выгрузки и справка всегда согласованы между собой.

Карта, запросы и выгрузки работают без входа. Учётная запись нужна только для
наблюдаемых областей: они по своей природе принадлежат конкретному человеку.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response as FastAPIResponse
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from service import __version__
from service.auth import (
    AuthError,
    AuthRequired,
    COOKIE_NAME,
    authenticate,
    current_user,
    open_session,
    register,
)
from service.config import SEVERITY_COLORS, SEVERITY_LABELS, Settings, WEB_ROOT
from service.db import Database, StorageError
from service.export import burns_geojson, hotspots_geojson, report_csv, report_json, report_kml
from service.geocode import GeocodingError, PhotonSearch
from service.geo import GeometryError, parse_bbox, parse_geometry
from service.live import FirmsClient
from service.llm import AnalysisStats, LLMAnalysis, LLMError
from service.query import QueryError, QuerySpec, parse_dates, run_query
from service.satellite import MAX_EVENT_DAYS, MAX_TILES, SatellitePredictor
from service.sources import DataSourceError, FeatureStore

BBOX_EXAMPLE = "38.90,45.00,39.10,45.12"
MAX_AREAS_PER_USER = 50


class QueryBody(BaseModel):
    """Тело POST /api/query. Область задаётся полигоном либо, как запасной вариант, bbox."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    date_from: str = Field(alias="from", description="Начало периода, ГГГГ-ММ-ДД")
    date_to: str = Field(alias="to", description="Конец периода, ГГГГ-ММ-ДД")
    geometry: dict[str, Any] | None = Field(default=None, description="GeoJSON Polygon/MultiPolygon")
    bbox: str | list[float] | None = Field(default=None, description="minLon,minLat,maxLon,maxLat")
    live: bool = Field(default=False, description="Добавить живые термоточки NASA FIRMS")


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str
    password: str


class ZoneBody(BaseModel):
    """Район наблюдения для мониторинга."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=80)
    geometry: dict[str, Any] | None = None
    bbox: str | list[float] | None = None


class AreaBody(BaseModel):
    """Сохранение наблюдаемой области."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    date_from: str = Field(alias="from")
    date_to: str = Field(alias="to")
    geometry: dict[str, Any] | None = None
    bbox: str | list[float] | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.query_mode not in {"satellite", "archive"}:
        raise ValueError("FIRES_QUERY_MODE must be satellite or archive")
    if settings.query_mode == "archive" and settings.data_dir is None:
        raise ValueError(
            "Режим archive показывает только подготовленные прогнозы: укажите каталог "
            "через --data-dir или FIRES_DATA_DIR. Встроенных демонстрационных данных нет."
        )
    store = FeatureStore(settings.data_dir) if settings.data_dir is not None else None
    predictor = SatellitePredictor(settings)
    database = Database(settings.db_path)
    external_apis_enabled = settings.query_mode == "satellite"
    firms = FirmsClient(
        settings.firms_map_key if external_apis_enabled else "",
        disabled_reason="Внешние API отключены в конфиге модели (service.use_external_apis)" if not external_apis_enabled else None,
    )
    geocoder = PhotonSearch()
    # Модель проверяется один раз при запуске: без весов прогноз невозможен, и
    # интерфейс должен сказать об этом сразу, а не после загрузки снимков.
    model_problem = predictor.model_problem() if settings.query_mode == "satellite" else None
    fire_first = None
    fire_first_default = False
    if settings.query_mode == "satellite":
        from service.fire_first import FireFirstPredictor

        fire_first = FireFirstPredictor(settings)
        fire_first_default = bool(fire_first.config.get("enabled", True))
    mask_bootstrap = None
    if fire_first is not None:
        from service.config import PROJECT_ROOT
        from service.fire_evidence import PersistentSources
        from service.persistent_mask import MaskBootstrap

        persistent_cfg = fire_first.config["persistent_sources"]
        mask_files = [PROJECT_ROOT / path for path in persistent_cfg["files"]]

        def _reload_mask() -> None:
            fire_first.persistent = PersistentSources(mask_files, persistent_cfg["radius_m"])

        mask_bootstrap = MaskBootstrap(
            mask_files, PROJECT_ROOT / persistent_cfg["region"] if persistent_cfg.get("region") else None,
            fire_first.config, settings.firms_map_key, settings.satellite_cache_dir.parent / "firms-cache",
            _reload_mask)
    monitor = None
    if fire_first is not None and fire_first.config.get("monitor", {}).get("enabled"):
        from service.monitor import FireMonitor

        monitor = FireMonitor(settings.satellite_cache_dir.parent / "monitor" / "state.json",
                              fire_first, fire_first.config["monitor"], weather=fire_first.weather)
    model_name = None
    if settings.query_mode == "satellite" and model_problem is None:
        from src.config import load_config

        model_name = load_config(settings.model_config)["burn_severity"]["model"]
    llm = LLMAnalysis(settings)

    app = FastAPI(
        title="Мониторинг природных пожаров",
        description=(
            "Прогноз гарей по спутниковым снимкам выбранной области и периода. "
            "Площадь считается в метрической проекции, координаты выдаются в EPSG:4326. "
            "Наблюдаемые области требуют входа, остальное доступно без учётной записи."
        ),
        version=__version__,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.predictor = predictor
    app.state.fire_first = fire_first
    app.state.monitor = monitor
    app.state.db = database
    app.state.firms = firms
    app.state.geocoder = geocoder
    app.state.llm = llm

    @app.on_event("startup")
    def _start_mask_bootstrap() -> None:
        if mask_bootstrap is not None and settings.build_mask:
            mask_bootstrap.start()

    @app.on_event("startup")
    def _start_monitor() -> None:
        if monitor is not None and settings.monitor_background and fire_first.archive.enabled:
            monitor.start()

    @app.on_event("shutdown")
    def _close_database() -> None:
        if monitor is not None:
            monitor.stop()
        database.close()

    # -------------------------------------------------- Обработка ошибок

    @app.exception_handler(GeometryError)
    @app.exception_handler(QueryError)
    @app.exception_handler(AuthError)
    async def _bad_request(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(StorageError)
    async def _storage(request: Request, error: Exception) -> JSONResponse:
        text = str(error)
        status = 404 if "не найдена" in text else 409
        return JSONResponse({"detail": text}, status_code=status)

    @app.exception_handler(AuthRequired)
    async def _unauthorized(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=401)

    @app.exception_handler(DataSourceError)
    async def _bad_source(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": f"Источник данных недоступен: {error}"}, status_code=503)

    @app.exception_handler(GeocodingError)
    async def _bad_geocoder(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=503)

    # ---------------------------------------------------- Разбор запроса

    def spec_from_params(
        date_from: Annotated[str, Query(alias="from", description="Начало периода, ГГГГ-ММ-ДД")],
        date_to: Annotated[str, Query(alias="to", description="Конец периода, ГГГГ-ММ-ДД")],
        bbox: Annotated[str, Query(description=f"minLon,minLat,maxLon,maxLat, например {BBOX_EXAMPLE}")],
    ) -> QuerySpec:
        start, end = parse_dates(date_from, date_to, settings.max_period_days)
        return QuerySpec(area=parse_bbox(bbox), date_from=start, date_to=end, source="bbox")

    # Depends задаётся значением по умолчанию: при `from __future__ import annotations`
    # локальный псевдоним типа не резолвится в глобалях модуля.
    spec_dependency = Depends(spec_from_params)

    def spec_from_body(body: QueryBody | AreaBody) -> QuerySpec:
        start, end = parse_dates(body.date_from, body.date_to, settings.max_period_days)
        if (body.geometry is None) == (body.bbox is None):
            raise GeometryError("Укажите ровно одно из полей: geometry или bbox")
        if body.geometry is not None:
            return QuerySpec(area=parse_geometry(body.geometry, settings.max_vertices),
                             date_from=start, date_to=end, source="polygon")
        raw = body.bbox if isinstance(body.bbox, str) else ",".join(str(v) for v in body.bbox)
        return QuerySpec(area=parse_bbox(raw), date_from=start, date_to=end, source="bbox")

    def execute(spec: QuerySpec, live: bool = False) -> dict[str, Any]:
        if settings.query_mode == "satellite":
            if model_problem:
                raise DataSourceError(model_problem)
            if not predictor.configured:
                raise DataSourceError("Для прогноза нужны CDSE_CLIENT_ID и CDSE_CLIENT_SECRET "
                                      "или локальный файл .secrets/cdse.env")
            if fire_first is not None and fire_first_default:
                dataset, prediction = fire_first.predict(spec)
            else:
                dataset, prediction = predictor.predict(spec)
            result = run_query(dataset, spec, filter_dates=False)
            result["prediction"] = prediction
        else:
            dataset = store.get()
            result = run_query(dataset, spec)
        result["data_updated_at"] = dataset.updated_at
        result["data_source"] = _data_source()
        if live:
            result["live"] = _live_layer(spec)
        return result

    def _data_source() -> dict[str, Any]:
        """Происхождение данных ответа; попадает и в выгрузки."""
        if settings.query_mode == "satellite":
            return {"kind": "satellite", "synthetic": False,
                    "description": "Прогноз модели по снимкам Sentinel-2 (Copernicus Data Space)"}
        return {"kind": "files", "synthetic": False,
                "description": "Подготовленные файлы прогнозов моделей"}

    def _live_layer(spec: QuerySpec) -> dict[str, Any]:
        """Живые термоточки FIRMS поверх подготовленных данных.

        Ошибка внешнего источника не прерывает запрос: поле error объясняет,
        почему живых данных нет, а карта продолжает показывать файлы моделей.
        """
        outcome = firms.fetch(spec.area.bounds, settings.live_day_range)
        inside = [
            feature
            for feature in outcome.features
            if spec.area.intersects(_point(feature))
        ]
        payload = outcome.as_dict()
        payload["count"] = len(inside)
        payload["hotspots"] = {"type": "FeatureCollection", "features": inside}
        return payload

    def require_user(session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None) -> dict[str, Any]:
        user = current_user(database, session)
        if user is None:
            raise AuthRequired("Требуется вход")
        return user

    user_dependency = Depends(require_user)
    optional_session = Cookie(default=None, alias=COOKIE_NAME)

    # ------------------------------------------------------------- API

    @app.post("/api/analysis/llm", tags=["Аналитика"], summary="Текстовый разбор агрегированных показателей")
    def analyze_with_llm(body: AnalysisStats, request: Request) -> dict[str, str]:
        try:
            answer = llm.analyze(body, request.client.host if request.client else "unknown")
        except LLMError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        return {"text": answer, "provider": llm.provider}

    @app.get("/api/health", tags=["Служебное"], summary="Состояние сервиса и состав данных")
    def health() -> dict[str, Any]:
        base = {
            "status": "ok",
            "version": __version__,
            "llm": {"enabled": bool(settings.llm_api_key)},
            "severity": [
                {"severity": key, "label": label, "color": SEVERITY_COLORS[key]}
                for key, label in SEVERITY_LABELS.items()
            ],
            "live": {
                "enabled": firms.enabled,
                "provider": "NASA FIRMS",
                "day_range": settings.live_day_range,
                "detail": None if firms.enabled else firms.disabled_reason,
                "note": "Термоточки VIIRS с задержкой около 3 часов. Контуры гарей в реальном "
                        "времени невозможны: Sentinel-2 возвращается на точку раз в 5 дней.",
            },
            "limits": {
                "max_vertices": settings.max_vertices,
                "max_period_days": settings.max_period_days,
                "max_areas": MAX_AREAS_PER_USER,
            },
        }
        if settings.query_mode == "satellite":
            today = date.today()
            return {
                **base,
                "source": {
                    "kind": "satellite", "directory": "Copernicus Data Space",
                    "files": [], "updated_at": None,
                },
                "counts": {"hotspots": 0, "burns": 0},
                "date_range": {"from": None, "to": None},
                "suggested_period": {
                    "from": (today - timedelta(days=30)).isoformat(),
                    "to": (today - timedelta(days=16)).isoformat(),
                },
                "fire_first": {
                    "enabled": fire_first_default,
                    "max_area_side_km": fire_first.config["events"]["max_area_side_km"] if fire_first else None,
                    "firms_configured": bool(settings.firms_map_key),
                    "persistent_mask": mask_bootstrap.state if mask_bootstrap else None,
                },
                "prediction": {
                    "enabled": predictor.configured and model_problem is None,
                    "model": model_name,
                    "provider": "Copernicus Data Space",
                    "max_event_days": MAX_EVENT_DAYS,
                    "max_tiles": MAX_TILES,
                    "detail": model_problem if predictor.configured and model_problem else
                    None if predictor.configured else
                    "Нужны CDSE_CLIENT_ID и CDSE_CLIENT_SECRET",
                },
            }
        try:
            dataset = store.get()
        except DataSourceError as error:
            return {**base, "status": "error", "detail": str(error)}
        earliest, latest = dataset.date_range
        return {
            **base,
            "source": {
                "kind": "files",
                "directory": dataset.origin,
                "files": list(dataset.files),
                "updated_at": dataset.updated_at,
            },
            "counts": {"hotspots": len(dataset.hotspots), "burns": len(dataset.burns)},
            "date_range": {"from": earliest, "to": latest},
        }

    @app.post("/api/reload", tags=["Служебное"], summary="Перечитать файлы данных")
    def reload_data() -> dict[str, Any]:
        if settings.query_mode == "satellite":
            return {"status": "ok", "mode": "satellite",
                    "detail": "Новые снимки выбираются при каждом новом запросе области"}
        dataset = store.reload()
        return {
            "status": "ok",
            "counts": {"hotspots": len(dataset.hotspots), "burns": len(dataset.burns)},
            "files": list(dataset.files),
            "updated_at": dataset.updated_at,
        }

    @app.get("/api/regions", tags=["Запрос"], summary="Регионы, названные в загруженных данных")
    def regions() -> dict[str, Any]:
        if settings.query_mode == "satellite":
            return {"regions": []}
        grouped: dict[str, dict[str, Any]] = {}
        dataset = store.get()
        for feature in (*dataset.hotspots, *dataset.burns):
            name = feature.extra.get("region")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            key = name.casefold()
            west, south, east, north = feature.geometry.bounds
            if key not in grouped:
                grouped[key] = {"name": name, "bbox": [west, south, east, north], "count": 0}
            entry = grouped[key]
            bounds = entry["bbox"]
            bounds[0] = min(bounds[0], west)
            bounds[1] = min(bounds[1], south)
            bounds[2] = max(bounds[2], east)
            bounds[3] = max(bounds[3], north)
            entry["count"] += 1
        return {"regions": sorted(grouped.values(), key=lambda entry: entry["name"].casefold())}

    @app.get("/api/demo-cases", tags=["Запрос"], summary="Реальные случаи для показа")
    def demo_cases() -> dict[str, Any]:
        """Области и периоды реальных пожаров (configs/demo_cases.yaml); результат считает сервис."""
        import yaml

        path = settings.service_config.parent / "demo_cases.yaml"
        if settings.query_mode != "satellite" or not path.is_file():
            return {"cases": []}
        with path.open(encoding="utf-8") as stream:
            return {"cases": (yaml.safe_load(stream) or {}).get("cases", [])}

    @app.get("/api/places", tags=["Запрос"], summary="Поиск городов, регионов и стран")
    def places(q: Annotated[str, Query(min_length=2, max_length=100)]) -> dict[str, Any]:
        query = q.strip()
        if len(query) < 2:
            return {"places": []}
        return {"places": geocoder.search(query)}

    @app.get("/api/query", tags=["Запрос"], summary="Термоточки, контуры гари и справка по bbox")
    def query_get(
        spec: QuerySpec = spec_dependency,
        live: Annotated[bool, Query(description="Добавить живые термоточки NASA FIRMS")] = False,
    ) -> dict[str, Any]:
        return execute(spec, live)

    @app.post("/api/query", tags=["Запрос"], summary="То же по GeoJSON-полигону")
    def query_post(body: QueryBody) -> dict[str, Any]:
        return execute(spec_from_body(body), body.live)

    @app.post("/api/area-preview", tags=["Наблюдение"],
              summary="Лёгкая выборка для мини-карты сохранённой области")
    def area_preview(body: QueryBody) -> dict[str, Any]:
        spec = spec_from_body(body)
        if settings.query_mode != "archive":
            return {"hotspots": [], "burns": [], "prediction_available": False}
        result = run_query(store.get(), spec)
        return {"hotspots": result["hotspots"]["features"], "burns": result["burns"]["features"],
                "prediction_available": True}

    @app.get("/api/live", tags=["Запрос"], summary="Только живые термоточки NASA FIRMS")
    def live_only(
        bbox: Annotated[str, Query(description=f"minLon,minLat,maxLon,maxLat, например {BBOX_EXAMPLE}")],
        days: Annotated[int, Query(ge=1, le=5, description="Глубина запроса в сутках")] = 1,
    ) -> dict[str, Any]:
        area = parse_bbox(bbox)
        outcome = firms.fetch(area.bounds, days)
        inside = [f for f in outcome.features if area.intersects(_point(f))]
        payload = outcome.as_dict()
        payload["count"] = len(inside)
        payload["hotspots"] = {"type": "FeatureCollection", "features": inside}
        return payload

    # ------------------------------------------------------- Мониторинг

    def monitor_or_404():
        if monitor is None:
            raise HTTPException(status_code=404, detail="Мониторинг выключен (configs/service.yaml → monitor)")
        return monitor

    @app.get("/api/monitor", tags=["Мониторинг"], summary="Районы наблюдения и оповещения о новых очагах")
    def monitor_state() -> dict[str, Any]:
        return monitor_or_404().summary()

    @app.post("/api/monitor/zones", tags=["Мониторинг"], summary="Добавить район наблюдения и сразу проверить его")
    def monitor_add_zone(body: ZoneBody) -> dict[str, Any]:
        current = monitor_or_404()
        if (body.geometry is None) == (body.bbox is None):
            raise GeometryError("Укажите ровно одно из полей: geometry или bbox")
        if body.geometry is not None:
            area = parse_geometry(body.geometry, settings.max_vertices)
        else:
            raw = body.bbox if isinstance(body.bbox, str) else ",".join(str(v) for v in body.bbox)
            area = parse_bbox(raw)
        try:
            zone = current.add_zone(body.name, area)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        changed = current.check_zone(zone["id"])
        return {"zone": next(z for z in current.zones() if z["id"] == zone["id"]), "alerts": changed}

    @app.delete("/api/monitor/zones/{zone_id}", tags=["Мониторинг"], summary="Удалить район наблюдения")
    def monitor_remove_zone(zone_id: str) -> dict[str, Any]:
        try:
            monitor_or_404().remove_zone(zone_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Район не найден") from error
        return {"status": "ok"}

    @app.post("/api/monitor/check", tags=["Мониторинг"], summary="Проверить все районы сейчас")
    def monitor_check() -> dict[str, Any]:
        current = monitor_or_404()
        changed = current.check_all()
        return {"alerts": changed, **current.summary()}

    @app.post("/api/monitor/alerts/read", tags=["Мониторинг"], summary="Отметить оповещения прочитанными")
    def monitor_read(alert_id: Annotated[str | None, Query()] = None) -> dict[str, Any]:
        try:
            changed = monitor_or_404().mark_read(alert_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Оповещение не найдено") from error
        return {"changed": changed}

    # -------------------------------------------------------- Выгрузки

    @app.get(
        "/api/export/burns.geojson",
        tags=["Выгрузка"],
        summary="Контуры гари, EPSG:4326, атрибуты id/severity/area_ha",
    )
    def export_burns(
        spec: QuerySpec = spec_dependency,
        full: Annotated[bool, Query(description="Отдать все атрибуты, а не только обязательные")] = False,
    ) -> Response:
        return _geojson_response(burns_geojson(execute(spec), full_properties=full), "burns.geojson")

    @app.get("/api/export/hotspots.geojson", tags=["Выгрузка"], summary="Термоточки, EPSG:4326")
    def export_hotspots(spec: QuerySpec = spec_dependency) -> Response:
        return _geojson_response(hotspots_geojson(execute(spec)), "hotspots.geojson")

    @app.get("/api/export/fires.kml", tags=["Выгрузка"],
             summary="Контуры гари и точки огня в KML (Google Earth)")
    def export_kml(spec: QuerySpec = spec_dependency) -> Response:
        return _kml_response(report_kml(execute(spec)), "fires.kml")

    def event_preview(spec: QuerySpec, event_id: str) -> Response:
        from service.event_preview import PreviewError, render_event

        if fire_first is None or not fire_first_default or settings.query_mode != "satellite":
            raise HTTPException(status_code=404, detail="Снимки событий есть только в режиме «огонь прежде гари»")
        execute(spec)  # те же проверки и тот же кеш, что у запроса
        dataset, prediction = fire_first.predict(spec)
        try:
            image = render_event(dataset, prediction, event_id, settings.satellite_cache_dir,
                                 fire_first.config["scenes"]["cloud_classes"]
                                 + fire_first.config["scenes"]["nodata_classes"])
        except PreviewError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return Response(content=image, media_type="image/png",
                        headers={"Content-Disposition": f'inline; filename="{event_id}.png"'})

    @app.get("/api/events/{event_id}/preview.png", tags=["Выгрузка"],
             summary="Снимки события: Sentinel-2 до/после и контур гари (PNG)")
    def event_preview_get(event_id: str, spec: QuerySpec = spec_dependency) -> Response:
        return event_preview(spec, event_id)

    @app.post("/api/events/{event_id}/preview.png", tags=["Выгрузка"],
              summary="Снимки события по GeoJSON-полигону (PNG)")
    def event_preview_post(event_id: str, body: QueryBody) -> Response:
        return event_preview(spec_from_body(body), event_id)

    @app.get("/api/report.json", tags=["Выгрузка"], summary="Аналитическая справка в JSON")
    def report_as_json(spec: QuerySpec = spec_dependency) -> Response:
        return _json_response(report_json(execute(spec)), "report.json")

    @app.get("/api/report.csv", tags=["Выгрузка"], summary="Аналитическая справка в CSV")
    def report_as_csv(spec: QuerySpec = spec_dependency) -> Response:
        return _csv_response(report_csv(execute(spec)), "report.csv")

    @app.post("/api/export", tags=["Выгрузка"], summary="Выгрузка по GeoJSON-полигону")
    def export_post(
        body: QueryBody,
        kind: Annotated[Literal["burns", "hotspots", "report.json", "report.csv", "kml"], Query()] = "burns",
    ) -> Response:
        result = execute(spec_from_body(body))
        if kind == "kml":
            return _kml_response(report_kml(result), "fires.kml")
        if kind == "burns":
            return _geojson_response(burns_geojson(result), "burns.geojson")
        if kind == "hotspots":
            return _geojson_response(hotspots_geojson(result), "hotspots.geojson")
        if kind == "report.json":
            return _json_response(report_json(result), "report.json")
        return _csv_response(report_csv(result), "report.csv")

    # ------------------------------------------------- Учётные записи

    @app.post("/api/auth/register", tags=["Учётная запись"], summary="Регистрация")
    def auth_register(body: Credentials, response: FastAPIResponse) -> dict[str, Any]:
        user = register(database, body.login, body.password)
        _start_session(response, user["id"])
        return {"user": user}

    @app.post("/api/auth/login", tags=["Учётная запись"], summary="Вход")
    def auth_login(body: Credentials, response: FastAPIResponse) -> dict[str, Any]:
        user = authenticate(database, body.login, body.password)
        _start_session(response, user["id"])
        return {"user": user}

    @app.post("/api/auth/logout", tags=["Учётная запись"], summary="Выход")
    def auth_logout(response: FastAPIResponse, session: str | None = optional_session) -> dict[str, Any]:
        if session:
            database.close_session(session)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"status": "ok"}

    @app.get("/api/auth/me", tags=["Учётная запись"], summary="Текущий пользователь")
    def auth_me(session: str | None = optional_session) -> dict[str, Any]:
        return {"user": current_user(database, session)}

    # --------------------------------------------- Наблюдаемые области

    @app.get("/api/areas", tags=["Наблюдение"], summary="Список наблюдаемых областей")
    def areas_list(user: dict = user_dependency) -> dict[str, Any]:
        return {"areas": database.list_areas(user["id"])}

    @app.post("/api/areas", tags=["Наблюдение"], summary="Сохранить область")
    def areas_create(body: AreaBody, user: dict = user_dependency) -> dict[str, Any]:
        name = body.name.strip()
        if not 1 <= len(name) <= 80:
            raise GeometryError("Название: от 1 до 80 символов")
        spec = spec_from_body(body)  # проверка геометрии и дат до записи в базу
        if len(database.list_areas(user["id"])) >= MAX_AREAS_PER_USER:
            raise StorageError(f"Достигнут предел: {MAX_AREAS_PER_USER} областей")
        bbox = None if body.geometry else ",".join(str(round(v, 6)) for v in spec.area.bounds)
        return {"area": database.create_area(
            user["id"], name, body.geometry, bbox,
            spec.date_from.isoformat(), spec.date_to.isoformat(),
        )}

    @app.delete("/api/areas/{area_id}", tags=["Наблюдение"], summary="Удалить область")
    def areas_delete(area_id: int, user: dict = user_dependency) -> dict[str, Any]:
        database.delete_area(user["id"], area_id)
        return {"status": "ok"}

    @app.get("/api/areas/{area_id}/status", tags=["Наблюдение"],
             summary="Актуальный статус области и что изменилось с прошлого раза")
    def areas_status(
        area_id: int,
        live: Annotated[bool, Query(description="Добавить живые термоточки NASA FIRMS")] = False,
        remember: Annotated[bool, Query(description="Запомнить показанное как просмотренное")] = True,
        user: dict = user_dependency,
    ) -> dict[str, Any]:
        area = database.get_area(user["id"], area_id)
        spec = spec_from_body(AreaBody(
            name=area["name"], geometry=area["geometry"], bbox=area["bbox"],
            **{"from": area["from"], "to": area["to"]},
        ))
        result = execute(spec, live)
        summary = result["summary"]
        result["area"] = area
        result["changes"] = _changes(area["seen"], summary, result["data_updated_at"])
        if remember:
            database.remember_visit(
                user["id"], area_id, summary["hotspot_count"], summary["burn_count"],
                summary["total_area_ha"], result["data_updated_at"],
            )
        return result

    # ------------------------------------------------------- Интерфейс

    if WEB_ROOT.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

        @app.middleware("http")
        async def no_cache_static(request: Request, call_next):
            # Файлы отдаются локально и весят килобайты: кэш браузера только мешает демонстрации.
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(WEB_ROOT / "index.html", headers={"Cache-Control": "no-cache"})

    def _start_session(response: FastAPIResponse, user_id: int) -> None:
        token, _ = open_session(database, user_id)
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
            path="/",
        )

    return app


def _changes(seen: dict[str, Any] | None, summary: dict[str, Any], stamp: str | None) -> dict[str, Any]:
    """Что изменилось с прошлого просмотра области."""
    if not seen:
        return {"first_visit": True, "data_changed": True}
    hotspots = summary["hotspot_count"] - (seen.get("hotspots") or 0)
    burns = summary["burn_count"] - (seen.get("burns") or 0)
    area = round(summary["total_area_ha"] - (seen.get("area_ha") or 0.0), 2)
    return {
        "first_visit": False,
        "hotspots": hotspots,
        "burns": burns,
        "area_ha": area,
        # Данные могли обновиться, не изменив цифр именно в этой области.
        "data_changed": stamp != seen.get("data_stamp"),
        "nothing_new": hotspots == 0 and burns == 0 and abs(area) < 0.01,
    }


def _point(feature: dict[str, Any]):
    from shapely.geometry import Point

    longitude, latitude = feature["geometry"]["coordinates"]
    return Point(longitude, latitude)


def _attachment(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


def _geojson_response(payload: dict[str, Any], filename: str) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/geo+json; charset=utf-8",
        headers=_attachment(filename),
    )


def _json_response(payload: dict[str, Any], filename: str) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers=_attachment(filename),
    )


def _kml_response(body: str, filename: str) -> Response:
    return Response(content=body.encode("utf-8"), media_type="application/vnd.google-earth.kml+xml",
                    headers=_attachment(filename))


def _csv_response(body: str, filename: str) -> Response:
    # BOM: без него Excel с русской локалью открывает UTF-8 как cp1251.
    return Response(
        content=("\ufeff" + body).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers=_attachment(filename),
    )


#: Точка входа для `uvicorn service.app:app`.
app = create_app()
