#!/usr/bin/env python3
"""
main.py — Точка входа для OKX-бота версии 11.9.1

Шаги:
1. Загружаем переменные окружения и определяем, в каком режиме работаем (DEMO или LIVE).
2. Получаем ключи для подключения к OKX API.
3. Инициализируем REST-клиент (core/rest_client.py).
4. Синхронизируем время с сервером OKX и выводим результат в консоль.
   Логирование подключим позже.
"""

import os
import sys

from dotenv import load_dotenv
from core.rest_client import RestClient  # Шаг 3

def main():
    # Шаг 1: Загрузка переменных окружения
    load_dotenv()
    env = os.getenv("ENV", "DEMO").upper()
    print(f"[1] Режим работы: {env}")

    # Шаг 2: Получение API-кредитов
    api_key       = os.getenv("OKX_API_KEY")
    api_secret    = os.getenv("OKX_API_SECRET")
    api_passphrase = os.getenv("OKX_API_PASSPHRASE")

    if not all([api_key, api_secret, api_passphrase]):
        print("Ошибка: отсутствуют OKX_API_KEY, OKX_API_SECRET или OKX_API_PASSPHRASE в окружении")
        sys.exit(1)
    print("[2] API-ключи загружены из окружения")

    # Шаг 3: Инициализация REST-клиента
    client = RestClient(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=api_passphrase,
        demo=(env == "DEMO")
    )
    print("[3] REST-клиент OKX инициализирован")

    # Шаг 4: Синхронизация времени с OKX
    print("[4] Синхронизируем время с сервером OKX...")
    try:
        server_time = client.get_server_time()
        print(f"[4] Время сервера OKX: {server_time}")
        print("[4] Синхронизация времени успешно завершена")
    except Exception as e:
        print(f"[4] Ошибка при синхронизации времени: {e}")
        sys.exit(1)

    # TODO:
    # - подключить логику стратегий
    # - стартовать WebSocket-клиент
    # - обработку событий и ордер-менеджмент

if __name__ == "__main__":
    main()
