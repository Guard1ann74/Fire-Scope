"""Регистрация, вход и сессии.

Пароли хешируются scrypt из стандартной библиотеки, сессия — случайный токен
в куке HttpOnly со сроком жизни в таблице sessions. Внешних пакетов не требуется.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

from service.db import Database, StorageError

COOKIE_NAME = "fires_session"
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 256

# Параметры scrypt: ~64 МБ и доли секунды на проверку. Достаточно, чтобы перебор
# по украденной базе был дорогим, и не настолько много, чтобы вход подвисал.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32


def _maxmem(n: int, r: int) -> int:
    """scrypt требует 128 * N * r байт, а OpenSSL по умолчанию не даёт больше 32 МБ.

    Без явного maxmem вызов падает с «memory limit exceeded» ровно на рабочих параметрах.
    Запас вдвое покрывает служебные буферы библиотеки.
    """
    return 128 * n * r * 2


class AuthError(ValueError):
    """Некорректные учётные данные. Наверх уходит как 400."""


class AuthRequired(AuthError):
    """Действие требует входа. Наверх уходит как 401: фронт отличает это от ошибки ввода."""


def validate_login(login: Any) -> str:
    login = str(login or "").strip()
    if not LOGIN_PATTERN.fullmatch(login):
        raise AuthError(
            "Логин: от 3 до 32 символов, латинские буквы, цифры, точка, дефис или подчёркивание"
        )
    return login


def validate_password(password: Any) -> str:
    password = str(password or "")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Пароль короче {MIN_PASSWORD} символов")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"Пароль длиннее {MAX_PASSWORD} символов")
    return password


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_LEN, maxmem=_maxmem(SCRYPT_N, SCRYPT_R),
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = stored.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
            maxmem=_maxmem(int(n), int(r)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def register(db: Database, login: Any, password: Any) -> dict[str, Any]:
    login = validate_login(login)
    password = validate_password(password)
    return db.create_user(login, hash_password(password))


def authenticate(db: Database, login: Any, password: Any) -> dict[str, Any]:
    login = str(login or "").strip()
    row = db.find_user(login)
    # Хеш-заглушка при отсутствии пользователя: время ответа не выдаёт, есть ли такой логин.
    stored = row["password_hash"] if row else hash_password(secrets.token_hex(16))
    if not verify_password(str(password or ""), stored) or row is None:
        raise AuthError("Неверный логин или пароль")
    return {"id": row["id"], "login": row["login"], "created_at": row["created_at"]}


def open_session(db: Database, user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = db.open_session(user_id, token)
    return token, expires


def current_user(db: Database, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = db.session_user(token)
    if row is None:
        return None
    return {"id": row["id"], "login": row["login"], "created_at": row["created_at"]}


__all__ = [
    "AuthError",
    "AuthRequired",
    "COOKIE_NAME",
    "StorageError",
    "authenticate",
    "current_user",
    "hash_password",
    "open_session",
    "register",
    "validate_login",
    "validate_password",
    "verify_password",
]
