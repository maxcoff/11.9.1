import asyncio
import signal
import logging

from core.tpsl_monitor      import TpslMonitor
from core.logger            import logger
from core.config            import get
from core.prehedge_watcher  import PreHedgeWatcher
from core.position_snapshot import PositionSnapshot
from core.reinvest import ReinvestManager



class Orchestrator:
    def __init__(self, rest, ws_monitor, order_client, hedge_manager, task_manager, poll_interval: float = 1.0):
        self.rest           = rest
        self.ws_monitor     = ws_monitor
        self.order_client   = order_client
        self.hedge_manager  = hedge_manager
        self.task_manager   = task_manager

        self.inst = get("INSTRUMENT", "") or ""

        self.tpsl = TpslMonitor(self.rest, self.ws_monitor, order_client, task_manager)

    

        self.ws_monitor.add_algo_listener(self.tpsl.on_fill_event)
        self.tp_watcher = PreHedgeWatcher(self.rest, self.ws_monitor, self.hedge_manager)
        
        self.hedged = False
        self.running = True
        self.initialized = False
        self.paused = False

        self.poll_interval = poll_interval

        reinvest_flag = (get("REINVEST_HEDGE", "false") or "false").lower()
        self.reinvest_enabled = reinvest_flag in ("1", "true", "yes")
        pct = float(get("REINVEST_HEDGE_PERCENT") or "100")
        self.reinvest_pct = max(0.0, min(pct, 100.0)) / 100.0
        self.min_lot = float(get("MIN_LOT") or "0")
        self._reinvest_lock = asyncio.Lock()

        self.snapshot   = PositionSnapshot(rest, ws_monitor)
        #self.reinvestor = ReinvestManager(rest, ws_monitor, order_client, self.tpsl, task_manager)

        self.logger = logging.getLogger(__name__)

    async def run(self):
        # 1) WS connect
        try:
            await self.ws_monitor.connect()
            try:
                await asyncio.wait_for(self.ws_monitor.ws_ready.wait(), timeout=25)
                logger.info("✅ Все приватные каналы подписаны, стартуем стратегию")
            except asyncio.TimeoutError:
                logger.error("❌ WS подписки не подтвердились за время ожидания")
                return
        except Exception as e:
            self.logger.error("❌ WS подписки провалены, проверьте соединение", extra={"mode": "START"}, exc_info=e)
            raise

        # 2) Регистрируем все задачи в менеджере
        #self.task_manager.register("snapshot", "Поддержание снимка", lambda: self.snapshot.run())
        #self.task_manager.register("reinvest_loop", "Цикл реинвеста", lambda: self._reinvest_loop())
        self.task_manager.register("tp_watcher", "Pre-Hedge watcher", lambda: self.tp_watcher.run())
        self.task_manager.register("order_mode", "Order mode", lambda: self.order_client.run())
        self.task_manager.register("shutdown", "Shutdown handler", lambda: self._shutdown())
        self.task_manager.register("tpsl", "Монитор TPSL", self.tpsl._loop)
        self.task_manager.register("hedge_run", "Выполнить хедж", self.hedge_manager.run)
        self.task_manager.register("orchestr_pause", "Пауза основного цикла", self.pause())
        

        # 2.0. Первичная синхронизация
        #await self.snapshot.sync_from_rest()

        # 2.1. Запуск snapshot
        #self.task_manager.start("snapshot")

        # 2.2. TPSL        
        #self.task_manager.start("tpsl")
        #self.tpsl.start()

        # 2.3. Реинвест
        #self.task_manager.start("reinvest_loop")#

        # 3) graceful shutdown
        loop = asyncio.get_running_loop()
        def _shutdown_signal(*_):
            self.task_manager.start("shutdown")
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown_signal)
            except (NotImplementedError, AttributeError):
                signal.signal(sig, _shutdown_signal)
        loop.set_exception_handler(self._handle_loop_exception)

        self.logger.info("Orchestrator started", extra={"mode": "START"})

        # 4) Начальная логика выбора режима
        long_q, short_q = await self.get_positions_rest()
        if long_q > 0 and short_q > 0:
            self.hedged = True
            self.logger.info("Startup: TWO legs → TPSL mode", extra={"mode": "STARTUP"})
            await asyncio.sleep(self.poll_interval * 3)
            await self.tpsl.bootstrap()
        elif (long_q > 0) ^ (short_q > 0):
            self.logger.info("Startup: ONE leg → PRE-HEDGE mode", extra={"mode": "STARTUP"})
            await asyncio.sleep(self.poll_interval * 3)
            self.task_manager.start("tp_watcher")

        # 5) Основной цикл
        await asyncio.sleep(self.poll_interval * 5)
        prev_long, prev_short = None, None
        while self.running:
            try:
                long_q, short_q = await self.get_positions()

                #if self.paused:
                #    await asyncio.sleep(self.poll_interval * 3)   # даём передохнуть, чтобы не жрать CPU
                #    continue

                # ORDER mode
                    # определяем что ноги были, но закрылись обе - переводим режим в ORDER
                if self.hedged and prev_long and prev_short and long_q == 0 and short_q == 0:
                    self.logger.info("→ All legs closed → ORDER mode", extra={"mode": "ORCHESTRATOR"})
                    self.hedged = False
                prev_long, prev_short = long_q, short_q

                if not self.hedged and long_q == 0 and short_q == 0:                    
                    self.task_manager.stop("tp_watcher")
                    self.task_manager.stop("tpsl")
                    self.task_manager.start("order_mode")

                #if not self.tpsl.is_active():
                #    self.logger.warning("[TPSL] task not running – restarting", extra={"mode": "TPSL"})
                #    self.tpsl.start()

                if not self.initialized:
                    await asyncio.sleep(self.poll_interval * 3)
                    self.initialized = True
                    continue

                # INITIAL PRE-HEDGE
                elif not self.hedged and (long_q > 0) ^ (short_q > 0):
                    self.task_manager.stop("order_mode")
                    self.task_manager.start("tp_watcher")

                # RE-PRE-HEDGE mode
                elif self.hedged and (long_q > 0) ^ (short_q > 0):        # когда после хеджа осталась одна позиция
                    self.logger.info("→ TPSL → PRE-HEDGE RE mode", extra={"mode": "REPREHEDGE"})
                    self.hedged = False
                    self.task_manager.stop("tpsl")
                    self.task_manager.stop("tp_watcher")
                    await asyncio.sleep(2)
                    self.task_manager.start("tp_watcher")

                # TPSL mode
                elif not self.hedged and long_q > 0 and short_q > 0:
                    self.logger.info("Two positions → TPSL mode active", extra={"mode": "TPSL"})
                    self.task_manager.stop("order_mode")
                    self.task_manager.stop("tp_watcher")
                    self.task_manager.start("tpsl")
                    self.hedged = True

                elif self.hedged and long_q > 0 and short_q > 0:                    
                    if not self.task_manager.is_running("tpsl"):
                        self.task_manager.start("tpsl")
                        self.logger.info(" TPSL task lost somewhere... → start it there.", extra={"mode": "TPSL"})

                

            except Exception as e:
                self.logger.error(
                    "Uncaught exception in orchestrator loop",
                    extra={"mode": "ORCHESTRATOR", "errorCode": getattr(e, "code", "-")},
                    exc_info=e
                )
            await asyncio.sleep(self.poll_interval)

    

    def pause(self):
        self.paused = True
        print (f"Paused {self.paused}")

    def resume(self):
        self.paused = False

    async def get_positions_rest(self) -> tuple[float, float]:
        resp   = await self.rest.request(
            "GET",
            "/api/v5/account/positions",
            params={"instId": self.inst}
        )
        long_q = short_q = 0.0
        for p in resp.get("data", []):
            if p["posSide"] == "long":
                long_q = float(p.get("pos", 0))
            elif p["posSide"] == "short":
                short_q = float(p.get("pos", 0))
        return long_q, short_q

    async def get_positions(self) -> tuple[float, float]:
        l = await self.ws_monitor.get_position(self.inst, "long")
        s = await self.ws_monitor.get_position(self.inst, "short")
        return l, s
    


    
    
            

    # --------------------------------------------------
    # Получаем цену фактического TP-fill из последнего сообщения WS
    # --------------------------------------------------
    def _last_tp_price_from_fill(self) -> float:
        # TPSLMonitor уже получил fill-сообщение, где есть fillPx
        # Можно сохранить его прямо в TPSLMonitor в on_fill_event
        # и вернуть здесь.  Для краткости пусть будет заглушка:
        return self.tpsl.last_tp_fill_px or 0.0


    async def _shutdown(self):        
        logger.info("Shutdown initiated", extra={"mode":"SHUTDOWN"})
        self.running = False    
        await self.task_manager.cancel_all()  
        logger.info("Shutdown complete", extra={"mode":"SHUTDOWN"})


    def _handle_loop_exception(self, loop, context):
        exc = context.get("exception")
        logger.error("Loop-level exception",extra={"mode":"LOOP"},exc_info=exc)

    