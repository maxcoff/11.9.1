# reinvest_manager.py

import asyncio
import time
from logger import logger


class Reinvest:
    def __init__(self,
                 rest_client,
                 ws_monitor,
                 order_client,
                 tpsl_monitor,
                 instrument: str,
                 reinvest_enabled: bool,
                 reinvest_pct: float,
                 min_lot: float):
        self.rest          = rest_client
        self.ws_monitor    = ws_monitor
        self.order_client  = order_client
        self.tpsl          = tpsl_monitor
        self.inst          = instrument
        self.reinvest_enabled = reinvest_enabled
        self.reinvest_pct     = reinvest_pct
        self.min_lot          = min_lot
        self._running         = True
        self._reinvest_lock   = asyncio.Lock()

    async def run(self):
        while self._running:
            logger.debug("🔄 [REINVEST] waiting for TP fill event…")
            await self.tpsl.tp_filled_evt.wait()
            logger.info("✅ [REINVEST] TP fill event caught")

            ep, eq, tp = self.tpsl.last_tp_fill()
            logger.debug(f"[REINVEST] last_tp_fill returned ep={ep}, eq={eq}, tp={tp}")

            try:
                await self._maybe_reinvest(ep, eq, tp)
            except Exception as e:
                logger.error("❌ [REINVEST] error during reinvest", exc_info=e)
            finally:
                self.tpsl.tp_filled_evt.clear()

    async def _maybe_reinvest(self, entry_price, entry_qty, tp_price):
        if not self.reinvest_enabled:
            return

        async with self._reinvest_lock:
            if entry_qty <= 0 or tp_price <= 0 or entry_price <= 0:
                logger.debug("[REINVEST] invalid TP-fill, skipping")
                return

            pnl = (tp_price - entry_price) * entry_qty
            if pnl <= 0:
                logger.debug(f"[REINVEST] pnl={pnl:.8f} ≤ 0, skipping")
                return

            use_amount = pnl * self.reinvest_pct
            logger.debug(f"[REINVEST] reinvest amount = {use_amount:.8f}")

            long_q, short_q = await self.ws_monitor.get_positions(self.inst)
            hedge_side = "long" if long_q > 0 else "short" if short_q > 0 else None
            hedge_qty  = long_q if long_q > 0 else short_q if short_q > 0 else 0.0

            if hedge_side is None:
                logger.debug("[REINVEST] no hedge detected, skipping")
                return

            price = self.ws_monitor.get_mark_price(self.inst)
            if price <= 0:
                try:
                    snap = await self.rest.fetch_snapshots(self.inst)
                    bid  = float(snap["data"][0]["bids"][0][0])
                    ask  = float(snap["data"][0]["asks"][0][0])
                    price = (bid + ask) / 2
                except Exception as e:
                    logger.warning(f"[REINVEST] failed to fetch price: {e}")
                    return

            close_qty = min(hedge_qty, use_amount / price)
            if close_qty <= self.min_lot:
                logger.info("[REINVEST] close_qty below min_lot, skipping")
                return

            side = "sell" if hedge_side == "long" else "buy"
            try:
                await self.order_client.place_order(
                    side=side,
                    order_type="market",
                    qty=close_qty
                )
                logger.info(
                    f"[REINVEST] placed market order: {side} {close_qty:.8f}"
                )
            except Exception as e:
                logger.error(f"[REINVEST] order failed: {e}", exc_info=e)

    def stop(self):
        self._running = False
