# main.py

import sys
import asyncio
import uvicorn
import threading
import aiohttp
from aiohttp import ClientTimeout
import time

from core.config       import get, get_bool
from core.logger       import logger
from core.rest_client  import RestClient
from core.ws_monitor   import WSMonitor
from core.order_mode   import OrderClient
from core.hedge        import HedgeManager
from core.orchestrator import Orchestrator
from core.app_state import task_manager




# На Windows для aiodns
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def start_web():
    uvicorn.run("core.http_server:app", host="192.168.100.254", port=8000)


async def main():
    # 0) Режим работы
    use_demo = get_bool("USE_DEMO", False)

    # 1) Ключи и URL в зависимости от режима
    if use_demo:
        rest_url   = get("REST_URL_DEMO")    or get("REST_URL")
        ws_url     = get("WS_URL_DEMO")      or get("WS_URL")
        api_key    = get("API_KEY_DEMO")     or get("API_KEY")
        api_secret = get("API_SECRET_DEMO")  or get("API_SECRET")
        passphrase = get("PASSPHRASE_DEMO")  or get("PASSPHRASE")
    else:
        rest_url   = get("REST_URL")
        ws_url     = get("WS_URL")
        api_key    = get("API_KEY")
        api_secret = get("API_SECRET")
        passphrase = get("PASSPHRASE")

    # Фоллбэки
    rest_url = rest_url or "https://www.okx.com"
    ws_url   = ws_url   or "wss://ws.okx.com:8443/ws/v5/private"

    # 2) HTTP-сессия и RestClient
    timeout = ClientTimeout(connect=15, sock_read=10, total=15)
    connector = aiohttp.TCPConnector(limit=100, keepalive_timeout=30, enable_cleanup_closed=True)
    session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    rest_client = RestClient(
        api_key    = api_key    or "",
        api_secret = api_secret or "",
        passphrase = passphrase or "",
        base_url   = rest_url,
        use_demo   = use_demo
    )
    
    # 3) Синхронизируем время
    try:
        server_ts = await rest_client.sync_rest_time()
        local_ts  = int(time.time() * 1000)
        rest_client.time_offset = server_ts - local_ts
        logger.info(f"⏲️ Time offset set to {rest_client.time_offset} ms", extra={"mode":"START"})
        
        snap = await rest_client.fetch_snapshots(get("INSTRUMENT") or "")
        if snap:
            logger.info("📈 Initial snapshot fetched", extra={"mode":"START"})
        else:
            logger.warning("⚠️ Initial snapshot is empty", extra={"mode":"START"})
    except Exception as e:
        logger.warning(f"⏰ Initial time sync failed: {e}", extra={"mode":"START", "errorCode": "-"})
        await rest_client.close()

    # 4) WS-монитор
    ws_monitor = WSMonitor(
        rest_client,
        api_key=api_key or "",
        api_secret=api_secret or "",
        passphrase=passphrase or "",
        ws_url=ws_url
    )
    task_manager.register("ws_monitor", "WS соединение", lambda: ws_monitor.connect())
    task_manager.start("ws_monitor")
    

    # 5) Торговые клиенты
    order_client  = OrderClient(rest_client, ws_monitor, logger)
    hedge_manager = HedgeManager(rest_client, ws_monitor, logger)

    # 6) Оркестратор
    orchestrator = Orchestrator(
        rest_client,
        ws_monitor,
        order_client,
        hedge_manager,
        task_manager,
    )

    # 7) Веб-сервер в отдельном потоке
    

    web_thread = threading.Thread(target=start_web, daemon=True)
    web_thread.start()

    # 8) Оркестратор в TaskManager
    task_manager.register("orchestrator", "Основной цикл", lambda: orchestrator.run())
    try:
        task_manager.start("orchestrator")
        await task_manager.wait("orchestrator")

    except (KeyboardInterrupt, SystemExit):
        logger.info("⛔ Остановка по запросу пользователя")
        await task_manager.cancel_all()

    except Exception as e:
        logger.exception(f"💥 Критическая ошибка в оркестраторе: {e}")
        await task_manager.cancel_all()

    finally:
        try:
            await ws_monitor.disconnect()
        except Exception as e:
            logger.warning(f"Ошибка при ws_monitor.disconnect(): {e}")

        try:
            await rest_client.close()
        except Exception as e:
            logger.warning(f"Ошибка при rest_client.close(): {e}")

        try:
            await session.close()
        except Exception as e:
            logger.warning(f"Ошибка при session.close(): {e}")

async def debug_task_list():   # asyncio.create_task(debug_task_list())
    while True:
        print("=== TaskManager.list() ===")
        for entry in task_manager.list():
            print(f"{entry['name']}: {entry['state']}")
        print("==========================\n")
        print("task_manager id:", id(task_manager))
        await asyncio.sleep(15)
    

async def log_connector_stats(connector: aiohttp.TCPConnector, logger, interval: int = 60):
    """
    Логирует состояние TCPConnector.
    """
    while True:
        # Суммарное число всех сокетов в пуле по всем хостам
        pool_total = sum(len(conns) for conns in connector._conns.values())
        # Сокеты, занятые в данный момент
        acquired = len(connector._acquired)
        # Задачи, ожидающие доступ к соединению
        waiters = sum(len(wait_list) for wait_list in connector._waiters.values())

        logger.info(
            f"Connector stats: pool_total={pool_total}, "
            f"acquired={acquired}, waiters={waiters}, "
            f"limit={connector.limit}, limit_per_host={connector.limit_per_host}"
        )

        await asyncio.sleep(interval)

if __name__ == "__main__":    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user", extra={"mode":"MAIN"})
    except Exception as e:
        logger.exception(f"💥 Fatal error: {e}", extra={"mode":"MAIN"})
   
    
        
