# config.py

import os
from typing import Optional, TypeVar, Callable, Any
from dotenv import load_dotenv

# загрузка .env выполняется один раз
load_dotenv(override=True)

T = TypeVar("T")

def get(
    key: str,
    default: Optional[T] = None,
    cast: Callable[[str], T] = lambda x: x  # по умолчанию — строка
) -> Optional[T]:
    """
    Возвращает значение из os.environ[key] или default.
    При получении строкового значения применяет функцию `cast`.
    """
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return cast(val)
    except Exception:
        # не смогли преобразовать — возвращаем default
        return default


def get_int(key: str, default: int = 0) -> int:
    return get(key, default, int)  # type: ignore


def get_float(key: str, default: float = 0.0) -> float:
    return get(key, default, float)  # type: ignore


def get_bool(key: str, default: bool = False) -> bool:
    """
    Преобразует "true"/"1"/"yes" → True, всё остальное → False
    """
    def _cast_bool(x: str) -> bool:
        return x.strip().lower() in ("1", "true", "yes", "on")
    return get(key, default, _cast_bool)  # type: ignore
