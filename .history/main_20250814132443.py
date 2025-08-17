# main.py

import sys
import asyncio
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

from core.http_server import start_http_server, state 



# На Windows для aiodns
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def main():
    # 0) Режим работы
    use_demo = get_bool("USE_DEMO", False)

    
    

    # 1) Собираем ключи и URL в зависимости от режима
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

    # 2) HTTP-сессия и RestClient (он стартует фон-таск синка времени)
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
    #position_snapshot = None  # будет инициализирован позж
    # 3) синхронизируем время и вручную выставляем offset
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
        
        #asyncio.create_task(log_connector_stats(connector, logger, interval=60))  # дебаг количества соединений в сессии
        
    except Exception as e:
        logger.warning(
            f"⏰ Initial time sync failed: {e}", extra={"mode":"START","errorCode":"-"}        )
        await rest_client.close()

    # 4) WS-монитор
    ws_monitor = WSMonitor(
        rest_client,
        api_key=api_key or "",
        api_secret=api_secret or "",
        passphrase=passphrase or "",
        ws_url=ws_url
    )
    await ws_monitor.connect()

    # 5) Объявляем торговые клиенты разных режимов
    order_client  = OrderClient(rest_client, ws_monitor, logger)
    hedge_manager = HedgeManager(rest_client, ws_monitor, logger)

    # 6) Оркестратор
    
    orchestrator = Orchestrator(
        rest_client,
        ws_monitor,
        order_client,
        hedge_manager        
    )

    # 7) Запуск + гарантированное закрытие
    state.orchestrator = orchestrator    
    # <<< привязываем оркестратор к веб‑панели
    http_task = await start_http_server(state.orchestrator, logger=logger, host="192.168.100.254", port=8000, admin_token="секрет")
    #await orch.run()
    print("http_task:", id(http_task))
    print("ORCH state id:", id(state.orchestrator))
    
    orchestration_task: Optional[asyncio.Task] = None    
    try:
        # 1) запускаем единственную задачу оркестратора
        orchestration_task = asyncio.create_task(orchestrator.run())

        # 2) дожидаемся завершения (или прерывания)
        await orchestration_task

    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка по запросу пользователя")
        if orchestration_task and not orchestration_task.done():
            orchestration_task.cancel()
            # Игнорируем штатный CancelledError
            with suppress(asyncio.CancelledError):
                await orchestration_task

    except Exception as e:
        logger.exception("Критическая ошибка в оркестраторе: %s", e)
        if orchestration_task and not orchestration_task.done():
            orchestration_task.cancel()
            with suppress(asyncio.CancelledError):
                await orchestration_task

    finally:
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
   
    
        
