import asyncio
from decimal import Decimal
from typing import Tuple

from core.config import get
from core.logger import logger


class ReinvestManager:
    """
    Управляет реинвестом прибыли от TP исходной ноги
    в частичное закрытие хедж-ноги.
    """

    def __init__(
        self,
        rest,
        ws_monitor,
        order_client,
        tpsl_monitor,
    ):
        self.rest   = rest
        self.ws     = ws_monitor
        self.orders = order_client
        self.tpsl   = tpsl_monitor

        self.inst        = get("INSTRUMENT") or ""
        self.reinvest_p  = float(get("REINVEST_HEDGE_PERCENT") or 100) / 100
        self.min_lot     = float(get("MIN_LOT") or 0)

        # чтобы не запускать несколько ре-инвестов одновременно
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Единый публичный метод.
    # Вызывается из Orchestrator когда tpsl.tp_filled_evt сработал.
    # ------------------------------------------------------------------
    async def handle_tp_fill(self, snapshot: Tuple[Decimal, Decimal, Decimal,str]):
        """
        snapshot = (entry_price, entry_qty, tp_price)
        """
        async with self._lock:
            await self._do_reinvest(snapshot)

    # ------------------------------------------------------------------
    # Внутренние шаги
    # ------------------------------------------------------------------
    async def _do_reinvest(self, snapshot: Tuple[Decimal, Decimal, Decimal, str]):
        ep, eq, tp, side = map(Decimal, snapshot)
        realized_pnl = (tp - ep) * eq
        
        if side == "long":
            realized_pnl = (tp - ep) * eq
        else:                       # short
            realized_pnl = (ep - tp) * eq

        if realized_pnl <= 0:
            return        

        use_amount = realized_pnl * Decimal(str(self.reinvest_p))
        logger.info(
            f"[REINVEST] realized={realized_pnl} -> use={use_amount}",
            extra={"mode": "REINVEST"},
        )

        # 1. Остановить tpsl-цикл, чтобы он не мешал
        await self.tpsl.stop()

        try:
            # 2. Определить сторону и объём хеджа
            long_q = await self.ws.get_position(self.inst, "long")
            short_q = await self.ws.get_position(self.inst, "short")
            if long_q > 0 and short_q == 0:
                hedge_side, hedge_qty = "long", long_q
            elif short_q > 0 and long_q == 0:
                hedge_side, hedge_qty = "short", short_q
            else:
                logger.warning("[REINVEST] нет ровно одной хедж-ноги")
                return

            # 3. Текущая цена для расчёта кол-ва лотов
            mark = self._mark_price()
            if mark <= 0:
                logger.warning("[REINVEST] mark-price не доступен")
                return

            close_qty = min(float(hedge_qty), float(use_amount / Decimal(str(mark))))
            if close_qty <= self.min_lot:
                logger.info("[REINVEST] close_qty <= min_lot, пропуск")
                return

            # 4. Рыночный ордер на частичное закрытие
            side_ord = "sell" if hedge_side == "long" else "buy"
            await self.orders.place_order(
                side=side_ord,
                order_type="market",
                qty=close_qty,
                pos_side=hedge_side,
            )
            logger.info(
                f"[REINVEST] отправлен market {side_ord} {close_qty}",
                extra={"mode": "REINVEST"},
            )

        finally:
            # 5. Запускаем tpsl заново (он пересчитает TP/SL на новый объём)
            self.tpsl.start()



    # ----------------------------------------------------------
    def _mark_price(self) -> float:
        """WS-цена или 0, если ещё не пришла."""
        return self.ws.get_mark_price(self.inst)