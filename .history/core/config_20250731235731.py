# core/config.py

import os
from typing import Optional, TypeVar, Callable, Dict, Mapping
from dotenv import dotenv_values

T = TypeVar("T")
_env_path = ".env"

def _read_env_file() -> Mapping[str, Optional[str]]:
    # возвращает словарь со всеми переменными из .env
    return dotenv_values(_env_path)  # читаем файл под капотом

def get(
    key: str,
    default: Optional[T] = None,
    cast: Callable[[str], T] = lambda x: x
) -> Optional[T]:
    # сначала пробуем из os.environ,
    # если нет — читаем из .env
    val = os.environ.get(key)
    if val is None:
        val = _read_env_file().get(key)

    if val is None:
        return default
    try:
        return cast(val)
    except Exception:
        return default

def get_int(key: str, default: int = 0) -> int:
    return get(key, default, int)  # type: ignore

def get_float(key: str, default: float) -> float:
    #return get(key, default, float)  # type: ignore
    try:
        return float(os.getenv(key, default))
    except ValueError:
        return default

def get_bool(key: str, default: bool = False) -> bool:
    def _cast_bool(x: str) -> bool:
        return x.strip().lower() in ("1", "true", "yes", "on")
    return get(key, default, _cast_bool)  # type: ignore
