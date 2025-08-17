import asyncio
import signal
from decimal import Decimal , getcontext

from core.tpsl_monitor      import TpslMonitor
from core.logger            import logger
from core.config            import get
from core.prehedge_watcher  import PreHedgeWatcher
from core.position_snapshot import PositionSnapshot
from core.reinvest import ReinvestManager


class Orchestrator:
    def __init__(self,
                 rest,
                 ws_monitor,
                 order_client,
                 hedge_manager,
                 poll_interval: float = 1.0):
        self.rest           = rest
        self.ws_monitor     = ws_monitor
        self.order_client   = order_client
        self.hedge_manager  = hedge_manager

        self.inst: str      = get("INSTRUMENT", "") or ""

        self.tpsl           = TpslMonitor(self.rest, self.ws_monitor)
        # регистрируем on_fill_event как слушатель algo-order fills
        self.ws_monitor.add_algo_listener(self.tpsl.on_fill_event)
        # PreHedgeWatcher для отслеживания TP и запуска хеджа
        self.tp_watcher     = PreHedgeWatcher(self.rest,self.ws_monitor,self.hedge_manager)

        self._reinvest_task : asyncio.Task | None = None
        self.order_task     : asyncio.Task | None = None
        self.prehedge_task  : asyncio.Task | None = None

        self.hedged         = False
        self.running        = True
        self.initialized    = False
        # интервал опроса в секундах
        self.poll_interval  = poll_interval

        # режим реинвеста прибыли в хедж
        reinvest_flag = (get("REINVEST_HEDGE", "false") or "false").lower()
        self.reinvest_enabled = reinvest_flag in ("1", "true", "yes")
        # процент прибыли, пущенный на закрытие хеджа (0-100)
        pct = float(get("REINVEST_HEDGE_PERCENT") or "100")
        self.reinvest_pct = max(0.0, min(pct, 100.0)) / 100.0
        # минимальный лот для частичного закрытия
        self.min_lot = float(get("MIN_LOT") or "0")
        # лочим одновременное reinvest-задачи
        self._reinvest_lock = asyncio.Lock()

        logger.info(
            f"[REINVEST] enabled={self.reinvest_enabled}, "
            f"pct={self.reinvest_pct:.2%}, min_lot={self.min_lot}",
            extra={"mode":"REINVEST"}
        )
        self.snapshot   = PositionSnapshot(rest, ws_monitor)
        self.reinvestor = ReinvestManager(rest, ws_monitor, order_client, self.tpsl)

    async def run(self):
        # 1) WS-connect + TPSL + reinvest loop
        try:
            await self.ws_monitor.connect()
        except Exception as e:
            logger.error("WebSocket connect failed", extra={"mode": "START"}, exc_info=e)
            raise

        self.tpsl.start()
        #self._reinvest_task = asyncio.create_task(self._reinvest_loop())
        self._reinvest_task = asyncio.create_task(self._reinvest_proxy())

        # 2) graceful shutdown handlers
        loop = asyncio.get_running_loop()
        def _shutdown_signal(*_):
            asyncio.create_task(self._shutdown())
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown_signal)
            except (NotImplementedError, AttributeError):
                signal.signal(sig, _shutdown_signal)
        loop.set_exception_handler(self._handle_loop_exception)

        logger.info("Orchestrator started", extra={"mode": "START"})

        # 3) Стартовый снапшот портфеля (REST)
        long_q, short_q = await self.get_positions_rest()
        if long_q > 0 and short_q > 0:
            self.hedged = True
            logger.info("Startup detected TWO legs → enter TPSL mode", extra={"mode": "STARTUP"})
        elif (long_q > 0) ^ (short_q > 0):
            logger.info("Startup detected ONE leg → PRE-HEDGE mode", extra={"mode": "STARTUP"})
            self.prehedge_task = asyncio.create_task(self.tp_watcher.run())


        # 4) Основной цикл опроса
        prev_long, prev_short = None, None
        while self.running:
            try:
                long_q, short_q = await self.get_positions()                
                # ← Новая ветка: обе ноги закрыты, возвращаемся в ORDER
                if self.hedged  and prev_long and prev_short  and long_q == 0 and short_q == 0:
                    logger.info("→ All legs closed → back to ORDER mode", extra={"mode":"ORCHESTRATOR"})
                    self.hedged = False
                prev_long, prev_short = long_q, short_q

                # ensure TPSL monitor is alive
                if not self.tpsl.is_active():
                    logger.warning("[TPSL] task not running – restarting",extra={"mode": "TPSL"})
                    self.tpsl.start()

                # delay first few polls until initialization
                if not self.initialized:
                    await asyncio.sleep(self.poll_interval * 3)
                    self.initialized = True
                    continue

                # 🟨 ORDER mode: без позиций
                if not self.hedged and long_q == 0 and short_q == 0:
                    if not self.order_task or self.order_task.done():
                        logger.info("→ ORDER mode", extra={"mode":"ORDER"})
                        self.order_task = asyncio.create_task(
                            self.order_client.run()
                        )

                # ─── RE-PRE-HEDGE mode (after TPSL exit) ───
                elif self.hedged and (long_q > 0) ^ (short_q > 0):
                    logger.info("→ TPSL → PRE-HEDGE RE mode", extra={"mode": "REPREHEDGE"})
                    self.hedged = False
                    # restart watcher
                    if self.prehedge_task and not self.prehedge_task.done():
                        self.prehedge_task.cancel()
                        try:
                            await self.prehedge_task
                        except asyncio.CancelledError:                        
                            self.prehedge_task = None
                            pass  
                        await asyncio.sleep(0)
                    self.prehedge_task = asyncio.create_task(self.tp_watcher.run())

                # ─── INITIAL PRE-HEDGE mode (first single leg) ───                
                elif not self.hedged and (long_q > 0) ^ (short_q > 0):
                    # schedule watcher once
                    if not self.prehedge_task or self.prehedge_task.done():
                        logger.info("→ PRE-HEDGE mode", extra={"mode": "PREHEDGE"})
                        self.prehedge_task = asyncio.create_task( self.tp_watcher.run())

                # ─── TPSL mode ───
                elif long_q > 0 and short_q > 0 and not self.hedged:                    
                    logger.info("Two positions → TPSL mode active", extra={"mode": "TPSL"})
                       # cancel other tasks
                    if self.order_task and not self.order_task.done():
                        self.order_task.cancel()
                        await asyncio.sleep(0)
                    if self.prehedge_task and not self.prehedge_task.done():
                        self.prehedge_task.cancel()
                        try:
                            await self.prehedge_task
                        except asyncio.CancelledError:                            
                            self.prehedge_task = None
                            pass  # ок, prehedge_task завершена
                        await asyncio.sleep(0)
                    self.hedged = True             

            except Exception as e:
                logger.error(
                    "Uncaught exception in orchestrator loop",
                    extra={
                        "mode":      "ORCHESTRATOR",
                        "errorCode": getattr(e, "code", "-")
                    },
                    exc_info=e
                )
            await asyncio.sleep(self.poll_interval)
    
    

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
    
    async def _reinvest_proxy(self):
        while self.running:
            await self.tpsl.tp_filled_evt.wait()
            snapshot = self.tpsl._pending_snapshot
            self.tpsl._pending_snapshot = None
            self.tpsl.tp_filled_evt.clear()
            if snapshot:
                await self.reinvestor.handle_tp_fill(snapshot)


    async def _shutdown(self):
        if not self.running:
            return
        logger.info("Shutdown initiated", extra={"mode":"SHUTDOWN"})
        self.running = False

        if self.order_task and not self.order_task.done():
            self.order_task.cancel()
        if self._reinvest_task and not self._reinvest_task.done():
            self._reinvest_task.cancel()
        if self.prehedge_task and not self.prehedge_task.done():
            self.prehedge_task.cancel()

        await self.tpsl.stop()
        await self.ws_monitor.disconnect()
        await self.rest.close()

    def _handle_loop_exception(self, loop, context):
        exc = context.get("exception")
        logger.error("Loop-level exception",
                     extra={"mode":"LOOP"},
                     exc_info=exc)
