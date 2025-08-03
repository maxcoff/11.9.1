import asyncio
import signal
from decimal import Decimal , getcontext

from core.tpsl_monitor      import TpslMonitor
from core.logger            import logger
from core.config            import get
from core.prehedge_watcher  import PreHedgeWatcher


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

    async def run(self):
        # 1) WS-connect + TPSL + reinvest loop
        try:
            await self.ws_monitor.connect()
        except Exception as e:
            logger.error("WebSocket connect failed", extra={"mode": "START"}, exc_info=e)
            raise

        self.tpsl.start()
        self._reinvest_task = asyncio.create_task(self._reinvest_loop())

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
    
    async def _reinvest_loop(self):
        while self.running:
            logger.info("▶️ reinvest_loop running", extra={"mode":"REINVEST"})
            evt = self.tpsl.tp_filled_evt
            print(f"[ORCH] waiting on tp_filled_evt id={id(evt)}")
            await evt.wait()
            logger.info("▶️ tpsl.tp_filled_evt filled", extra={"mode":"REINVEST"})
            snapshot = self.tpsl._pending_snapshot
            self.tpsl._pending_snapshot = None
            self.tpsl.tp_filled_evt.clear()

            if snapshot is None:
                logger.warning("[REINVEST] no snapshot, skipping")

                continue

            # Конвертация в Decimal
            ep, eq, tp = snapshot
            ep = Decimal(str(ep))
            eq = Decimal(str(eq))
            tp = Decimal(str(tp))

            realized_pnl = (tp - ep) * eq  # Decimal * Decimal
            if realized_pnl <= 0:
                #debug
                print(f"[REINVEST] realized_pnl={realized_pnl:.8f} ≤ 0, skipping reinvest")
                continue

            reinvest_pct = Decimal(str(self.reinvest_pct))  # float → Decimal
            use_amount = realized_pnl * reinvest_pct         # Decimal * Decimal

            logger.debug(f"[REINVEST] use_amount={use_amount}")
            # логируем все ключевые величины
            logger.info(
                "[REINVEST] "
                f"ep={ep}, eq={eq}, tp={tp}, "
                f"realized_pnl={realized_pnl}, "
                f"reinvest_pct={reinvest_pct}, "
                f"use_amount={use_amount}"
            )
            # пытаемся выполнить реинвест и логируем ошибки
            try:
                await self._maybe_reinvest(ep, eq, tp)
            except Exception as e:
                logger.error("❌ [REINVEST] error during _maybe_reinvest", exc_info=e)

    async def _maybe_reinvest(self, entry_price, entry_qty, tp_price):
        if not self.reinvest_enabled:
            return

        async with self._reinvest_lock:
            # пропускаем, если ещё не было валидного TP-fill
            if entry_qty <= 0 or tp_price <= 0 or entry_price <= 0:
                logger.debug(
                    "[REINVEST] no valid TP-fill yet, skipping",
                    extra={"mode":"REINVEST"}
                )
                return

            # 1) Рассчитываем реализацию PnL и сумму для реинвеста
            realized_pnl = (tp_price - entry_price) * entry_qty
            if realized_pnl <= 0:
                logger.debug(
                    f"[REINVEST] realized_pnl={realized_pnl:.8f} ≤ 0, skipping",
                    extra={"mode":"REINVEST"}
                )
                return
            use_amount = realized_pnl * self.reinvest_pct

            logger.debug(
                f"[REINVEST] entry_price={entry_price:.8f}, "
                f"entry_qty={entry_qty:.8f}, tp_price={tp_price:.8f}, "
                f"realized_pnl={realized_pnl:.8f}, "
                f"reinvest_pct={self.reinvest_pct:.2%}, "
                f"use_amount={use_amount:.8f}",
                extra={"mode":"REINVEST"}
            )

            # 2) Текущее хедж-положение
            long_q, short_q = await self.get_positions()
            hedge_side = "long" if long_q > 0 else "short" if short_q > 0 else None
            hedge_qty  = long_q if long_q > 0 else short_q if short_q > 0 else 0.0

            logger.debug(
                f"[REINVEST] detected hedge_side={hedge_side}, hedge_qty={hedge_qty:.8f}",
                extra={"mode":"REINVEST"}
            )
            if hedge_side is None:
                return

            # 3) Берём актуальную цену для расчёта объёма закрытия
            hedge_price = self.ws_monitor.get_mark_price(self.inst)
            if hedge_price <= 0:
                logger.debug(
                    "[REINVEST] mark_price from WS is zero, fetching REST snapshot",
                    extra={"mode":"REINVEST"}
                )
                try:
                    snap = await self.rest.fetch_snapshots(self.inst)
                    bid  = float(snap["data"][0]["bids"][0][0])
                    ask  = float(snap["data"][0]["asks"][0][0])
                    hedge_price = (bid + ask) / 2
                except Exception as e:
                    logger.warning(
                        f"[REINVEST] REST snapshot failed: {e}",
                        extra={"mode":"REINVEST","errorCode":"-"}
                    )
                    return

            logger.debug(
                f"[REINVEST] hedge_price={hedge_price:.8f}",
                extra={"mode":"REINVEST"}
            )

            # 4) Расчёт объёма и фильтрация по min_lot
            close_qty = min(hedge_qty, use_amount / hedge_price) if hedge_price > 0 else 0.0
            logger.debug(
                f"[REINVEST] calculated close_qty={close_qty:.8f}, "
                f"min_lot={self.min_lot:.8f}",
                extra={"mode":"REINVEST"}
            )
            if close_qty <= self.min_lot:
                logger.info(
                    "[REINVEST] close_qty <= min_lot, skipping reinvest",
                    extra={"mode":"REINVEST"}
                )
                return

            # 5) Отправляем market-order
            side = "sell" if hedge_side == "long" else "buy"
            try:
                logger.debug(
                    f"[REINVEST] placing market order side={side}, qty={close_qty:.8f}",
                    extra={"mode":"REINVEST"}
                )
                await self.order_client.place_order(
                    side=side,
                    order_type="market",
                    qty=close_qty
                )
                logger.info(
                    f"[REINVEST] order placed: side={side}, qty={close_qty:.8f}",
                    extra={"mode":"REINVEST"}
                )
            except Exception as e:
                logger.error(
                    f"[REINVEST] place_order failed: {e}",
                    extra={"mode":"REINVEST","errorCode":getattr(e,"code","-")},
                    exc_info=e
                )



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
