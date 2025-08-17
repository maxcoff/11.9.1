# main.py

import sys
import asyncio
import uvicorn
import threading
import aiohttp
from aiohttp import ClientTimeout
import time

from contextlib import suppress
from typing import Optional
from core.config       import get, get_bool
from core.logger       import logger
from core.rest_client  import RestClient
# from core.tpsl_monitor import TPSLMonitor
from core.ws_monitor   import WSMonitor

from core.order_mode   import OrderClient
from core.hedge        import HedgeManager
from core.orchestrator import Orchestrator

from core.http_server import run_server
from core.task_manager import TaskManager

task_manager = TaskManager()


# На Windows для aiodns
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def start_web():
    uvicorn.run("core.http_server:app", host="192.168.100.254", port=8000)

async def main():
    # 0) Режим работы
    use_demo = get_bool("USE_DEMO", False)
    
    # 1 ) пишем loop id основного потока, потому что веб сервер работает в другом и не видит этого
    #loop = asyncio.get_running_loop()
    #task_manager.init_loop(loop)

    # 2) Собираем ключи и URL в зависимости от режима
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

    # Фоллбэки на случай, если в .env что-то не указано
    rest_url = rest_url or "https://www.okx.com"
    ws_url   = ws_url   or "wss://ws.okx.com:8443/ws/v5/private"

    # 3) HTTP-сессия и RestClient (он стартует фон-таск синка времени)
    timeout = ClientTimeout(connect=15, sock_read=10, total=15)
    connector = aiohttp.TCPConnector(limit=100, keepalive_timeout=30, enable_cleanup_closed=True)
    session = aiohttp.ClientSession(connector=connector,timeout=timeout)
    #logger.info(f"HTTP session created with timeout={timeout.total} seconds in MAIN", extra={"mode":"START"})
    rest_client = RestClient(
        api_key    = api_key    or "",
        api_secret = api_secret or "",
        passphrase = passphrase or "",
        #session    = session,
        base_url   = rest_url,
        use_demo   = use_demo    
    )
    
    # 4) синхронизируем время и вручную выставляем offset
    try:
        server_ts = await rest_client.sync_rest_time()
        local_ts  = int(time.time() * 1000)
        rest_client.time_offset = server_ts - local_ts
        logger.info(
            f"⏲️ Time offset set to {rest_client.time_offset} ms",
            extra={"mode":"START"}
        )        
        snap = await rest_client.fetch_snapshots(get("INSTRUMENT") or "")
        if snap:
            #logger.info(f"📈 Initial snapshot fetched: {snap}", extra={"mode":"START"})
            logger.info(f"📈 Initial snapshot fetched", extra={"mode":"START"})
        else:
            logger.warning("⚠️ Initial snapshot is empty", extra={"mode":"START"})
        
    except Exception as e:
        logger.warning(
            f"⏰ Initial time sync failed: {e}", extra={"mode":"START","errorCode":"-"}        )
        await rest_client.close()

    # 5) WS-монитор
    ws_monitor = WSMonitor(
        rest_client,
        api_key=api_key or "",
        api_secret=api_secret or "",
        passphrase=passphrase or "",
        ws_url=ws_url
    )    
    task_manager.register(
        name="ws_monitor",  # ключ из TRACKED_TASKS
        description="WS соединение",
        factory=lambda: ws_monitor.connect()
    )
      # сразу стартуем
    task_manager.start("ws_monitor")

    # 5) Объявляем торговые клиенты разных режимов
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

    # 7) Запуск + гарантированное закрытие
       # запуск web сервера в отдельном потоке, т.к. он не умеет async и блокирует поток
       # потом еще придется линковать основной поток, чтоб он видел задачи в нем
    web_thread = threading.Thread(target=start_web, daemon=True)    
    try:
        web_thread.start()
    except asyncio.CancelledError:
        pass
       
       # запуск основного цикла 
    #orchestration_task: Optional[asyncio.Task] = None        
    task_manager.register(name="orchestrator", description="Основной цикл", factory=lambda: orchestrator.run())
    try:    
        # 1) запускаем единственную задачу оркестратора
        #orchestration_task = asyncio.create_task(orchestrator.run())
        #orchestration_task = create_tracked_task(orchestrator.run(), "orchestrator")    
        task_manager.start("orchestrator")  

        # 2) дожидаемся завершения (или прерывания)
        await task_manager.wait("orchestrator")

    except (KeyboardInterrupt, SystemExit):
        logger.info(f"⛔ Остановка по запросу пользователя")
        await task_manager.cancel_all()            

    except Exception as e:
        logger.exception(f"💥 Критическая ошибка в оркестраторе: %s", e)
        await task_manager.cancel_all()

    finally:

        #   можно переписать под использование таск менеджера
        try:
            await ws_monitor.disconnect()
        except Exception as e:
            logger.warning("Ошибка при ws_monitor.disconnect(): %s", e)

        # try:
        #     await tpsl_monitor.stop()
        # except Exception as e:
        #     logger.warning("Ошибка при tpsl_monitor.stop(): %s", e)

        try:
            await rest_client.close()
        except Exception as e:
            logger.warning("Ошибка при rest_client.close(): %s", e)

        try:
            await session.close()
        except Exception as e:
            logger.warning("Ошибка при session.close(): %s", e)
    
    

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
   
    
        
