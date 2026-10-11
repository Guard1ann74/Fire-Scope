"""Информационно-аналитический сервис мониторинга природных пожаров."""

from __future__ import annotations

__all__ = ["__version__", "create_app"]

__version__ = "1.0.0"


def create_app(*args, **kwargs):
    """Фабрика приложения. Импорт отложен, чтобы `python -m service` не тянул FastAPI раньше времени."""
    from service.app import create_app as factory

    return factory(*args, **kwargs)
