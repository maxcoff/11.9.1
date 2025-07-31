# main.py

import sys
import asyncio
import aiohttp
import time

from core.config       import get, get_bool
from core.logger       import logger
from core.rest_client  import RestClient
#from ws_monitor   import WSMonitor
#from order_mode   import OrderClient
#from hedge        import HedgeManager
#from orchestrator import Orchestrator

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
    session     = aiohttp.ClientSession()
    rest_client = RestClient(
        api_key    = api_key    or "",
        api_secret = api_secret or "",
        passphrase = passphrase or "",
        session    = session,
        base_url   = rest_url,
        use_demo   = use_demo
    )
    # 3) синхронизируем время и вручную выставляем offset
    try:
        server_ts = await rest_client.sync_rest_time()
        local_ts  = int(time.time() * 1000)
        rest_client.time_offset = server_ts - local_ts
        logger.info(
            f"⏲️ Time offset set to {rest_client.time_offset} ms",
            extra={"mode":"START"}
        )
        # Закрываем сессию сразу после синхронизации
        await rest_client.close()
        logger.info("👋 REST session closed right after time sync", extra={"mode": "SYNC_TIME"})
        
    except Exception as e:
        logger.warning(
            f"⏰ Initial time sync failed: {e}", extra={"mode":"START","errorCode":"-"}        )
        await rest_client.close()
    

    # 4) (опционально) делаем первый стакан для проверки связи
    try:
        await rest_client.fetch_snapshots(get("INSTRUMENT") or "")
        logger.info("📈 Initial snapshot fetched", extra={"mode":"START"})
    except Exception as e:
        logger.warning(f"⚠️ Initial snapshot failed: {e}", extra={"mode":"START"})
        await rest_client.close()
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user", extra={"mode":"MAIN"})
        await rest_client.close()


    

if __name__ == "__main__":    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user", extra={"mode":"MAIN"})
    except Exception as e:
        logger.exception(f"💥 Fatal error: {e}", extra={"mode":"MAIN"})
    
        
