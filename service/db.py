"""Хранилище учётных записей и наблюдаемых областей на SQLite.

Файл базы создаётся при первом запуске, миграции не нужны: схема разворачивается
через CREATE TABLE IF NOT EXISTS. Стандартная библиотека, без внешних зависимостей.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    login         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS areas (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    geometry      TEXT,
    bbox          TEXT,
    date_from     TEXT NOT NULL,
    date_to       TEXT NOT NULL,
    annotation_key TEXT,
    created_at    TEXT NOT NULL,
    checked_at    TEXT,
    seen_hotspots INTEGER,
    seen_burns    INTEGER,
    seen_area_ha  REAL,
    seen_stamp    TEXT,
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_areas_user ON areas(user_id);
"""

SESSION_DAYS = 30


class StorageError(RuntimeError):
    """Нарушение правил хранилища: занятый логин, чужая область и подобное."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


class Database:
    """Тонкая обёртка над sqlite3 с потокобезопасным доступом.

    uvicorn выполняет синхронные обработчики в пуле потоков, поэтому соединение
    открывается с check_same_thread=False и закрывается блокировкой.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._connection.executescript(SCHEMA)
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(areas)")}
            if "annotation_key" not in columns:
                self._connection.execute("ALTER TABLE areas ADD COLUMN annotation_key TEXT")
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                yield cursor
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    # ------------------------------------------------------------ Учётки

    def create_user(self, login: str, password_hash: str) -> dict[str, Any]:
        moment = _iso(utc_now())
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "INSERT INTO users (login, password_hash, created_at) VALUES (?, ?, ?)",
                    (login, password_hash, moment),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError as error:
            raise StorageError("Такой логин уже занят") from error
        return {"id": user_id, "login": login, "created_at": moment}

    def find_user(self, login: str) -> sqlite3.Row | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE login = ?", (login,))
            return cursor.fetchone()

    def user_by_id(self, user_id: int) -> sqlite3.Row | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            return cursor.fetchone()

    # ------------------------------------------------------------ Сессии

    def open_session(self, user_id: int, token: str) -> str:
        now = utc_now()
        expires = now + timedelta(days=SESSION_DAYS)
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, _iso(now), _iso(expires)),
            )
        return _iso(expires)

    def session_user(self, token: str) -> sqlite3.Row | None:
        """Вернуть владельца живой сессии; просроченные удаляются на месте."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token = ? AND s.expires_at > ?",
                (token, _iso(utc_now())),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute("DELETE FROM sessions WHERE token = ? OR expires_at <= ?",
                               (token, _iso(utc_now())))
            return row

    def close_session(self, token: str) -> None:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))

    # --------------------------------------------- Наблюдаемые области

    def list_areas(self, user_id: int) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM areas WHERE user_id = ? ORDER BY datetime(created_at) DESC",
                (user_id,),
            )
            return [_area_to_dict(row) for row in cursor.fetchall()]

    def get_area(self, user_id: int, area_id: int) -> dict[str, Any]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM areas WHERE id = ? AND user_id = ?", (area_id, user_id))
            row = cursor.fetchone()
        if row is None:
            raise StorageError("Область не найдена")
        return _area_to_dict(row)

    def create_area(
        self,
        user_id: int,
        name: str,
        geometry: dict | None,
        bbox: str | None,
        date_from: str,
        date_to: str,
        annotation_key: str | None = None,
    ) -> dict[str, Any]:
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "INSERT INTO areas (user_id, name, geometry, bbox, date_from, date_to, annotation_key, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        name,
                        json.dumps(geometry, ensure_ascii=False) if geometry else None,
                        bbox,
                        date_from,
                        date_to,
                        annotation_key,
                        _iso(utc_now()),
                    ),
                )
                area_id = cursor.lastrowid
        except sqlite3.IntegrityError as error:
            raise StorageError("Область с таким названием уже сохранена") from error
        return self.get_area(user_id, area_id)

    def delete_area(self, user_id: int, area_id: int) -> None:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM areas WHERE id = ? AND user_id = ?", (area_id, user_id))
            if cursor.rowcount == 0:
                raise StorageError("Область не найдена")

    def remember_visit(
        self,
        user_id: int,
        area_id: int,
        hotspots: int,
        burns: int,
        area_ha: float,
        stamp: str | None,
    ) -> None:
        """Запомнить, что пользователь уже видел, — из этого считается дельта."""
        with self._cursor() as cursor:
            cursor.execute(
                "UPDATE areas SET checked_at = ?, seen_hotspots = ?, seen_burns = ?,"
                " seen_area_ha = ?, seen_stamp = ? WHERE id = ? AND user_id = ?",
                (_iso(utc_now()), hotspots, burns, area_ha, stamp, area_id, user_id),
            )


def _area_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "geometry": json.loads(row["geometry"]) if row["geometry"] else None,
        "bbox": row["bbox"],
        "from": row["date_from"],
        "to": row["date_to"],
        "annotation_key": row["annotation_key"],
        "created_at": row["created_at"],
        "checked_at": row["checked_at"],
        "seen": None
        if row["checked_at"] is None
        else {
            "hotspots": row["seen_hotspots"],
            "burns": row["seen_burns"],
            "area_ha": row["seen_area_ha"],
            "data_stamp": row["seen_stamp"],
        },
    }
