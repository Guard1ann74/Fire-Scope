"""Запуск сервиса: `python -m service`."""

from __future__ import annotations

import argparse
from dataclasses import replace
import sys
from pathlib import Path

from service.config import Settings, use_utf8_output
from service.sources import DataSourceError, load_dataset


def build_parser(defaults: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m service",
        description="Информационно-аналитический сервис мониторинга природных пожаров.",
    )
    parser.add_argument("--host", default=defaults.host, help=f"по умолчанию {defaults.host}")
    parser.add_argument("--port", type=int, default=defaults.port, help=f"по умолчанию {defaults.port}")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=defaults.data_dir,
        help="каталог с GeoJSON подготовленных прогнозов; включает режим archive",
    )
    parser.add_argument("--reload", action="store_true", help="перезапуск при изменении кода")
    return parser


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    use_utf8_output()
    defaults = Settings.from_env()
    args = build_parser(defaults).parse_args(argv)
    settings = replace(defaults, data_dir=args.data_dir, host=args.host, port=args.port)
    if args.data_dir is not None and args.data_dir != defaults.data_dir:
        settings = replace(settings, query_mode="archive")
    if settings.query_mode == "archive" and settings.data_dir is None:
        print("Режим archive требует --data-dir с подготовленными прогнозами: "
              "встроенных демонстрационных данных нет.", file=sys.stderr)
        return 1

    if settings.query_mode == "archive":
        try:
            dataset = load_dataset(settings.data_dir)
        except DataSourceError as error:
            print(f"Ошибка данных: {error}", file=sys.stderr)
            return 1
        print(f"Данные (архив): {settings.data_dir}")
        print(f"  термоточек: {len(dataset.hotspots)}, контуров гари: {len(dataset.burns)}")
    else:
        print("Данные: спутниковые снимки и прогноз модели по выбранной области")
        from service.satellite import SatellitePredictor

        print(f"  Модель гари: {settings.model_config}")
        problem = SatellitePredictor(settings).model_problem()
        if problem:
            print(f"  Внимание: {problem}", file=sys.stderr)
        if not (settings.cdse_client_id and settings.cdse_client_secret):
            print("  Внимание: нет CDSE_CLIENT_ID/CDSE_CLIENT_SECRET — прогноз по снимкам "
                  "будет недоступен до их настройки (.secrets/cdse.env).", file=sys.stderr)
    print(f"Интерфейс:    http://{settings.host}:{settings.port}/")
    print(f"Документация: http://{settings.host}:{settings.port}/docs")

    if args.reload:
        # uvicorn с --reload требует строку импорта, а не объект приложения.
        import os

        os.environ["FIRES_QUERY_MODE"] = settings.query_mode
        if settings.data_dir is not None:
            os.environ["FIRES_DATA_DIR"] = str(settings.data_dir)
        uvicorn.run("service.app:app", host=settings.host, port=settings.port, reload=True)
    else:
        from service.app import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
