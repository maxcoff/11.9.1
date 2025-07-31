import asyncio
from decimal import Decimal

from core.config import get
from core.logger import logger


class PreHedgeWatcher:
    def __init__(self, rest, ws_monitor, hedge_manager):
        self.rest          = rest
        self.ws            = ws_monitor
        self.hedge_manager = hedge_manager

        self.inst          = get("INSTRUMENT", "") or ""
        # Порог убыточности в долях (например, 0.01 для 1%)
        self.threshold     = float(get("HEDGE_LOSS_TRIGGER_PERCENT", "0.5") or "0.5") / 100

        self.tp_active     = False
        # Событие наступления loss-триггера
        self._loss_evt     = asyncio.Event()

        # Заполнятся в _wait_for_position
        self.entry_price: Decimal | None = None
        self.side:       str     | None = None  # "long" или "short"
        self.qty:        float   | None = None

    async def run(self):
        # 1) Подписываемся на тикеры
        self.ws.add_ticker_listener(self._on_ticker)

        # 2) Ждём, пока появится ровно одна нога позы
        await self._wait_for_position()

        # 3) Сразу выставляем TP, если ещё не выставлен
        if not self.tp_active:
            await self._place_tp()
            self.tp_active = True

        # 4) Ждём loss-триггер
        logger.info(
            f"⏳ [PreHEDGE] waiting for loss ≤ −{self.threshold*100:.2f}%"
        )
        await self._loss_evt.wait()

        logger.info("🚨 [PreHEDGE] loss trigger hit → starting hedge")
        await self.hedge_manager.run()

    async def _wait_for_position(self):
        while True:
            lq = await self.ws.get_position(self.inst, "long")
            sq = await self.ws.get_position(self.inst, "short")

            # ровно одна нога
            if lq > 0 and sq == 0:
                self.side        = "long"
                self.qty         = lq
                self.entry_price = Decimal(
                    await self.ws.get_entry_price(self.inst, "long")
                )
                return

            if sq > 0 and lq == 0:
                self.side        = "short"
                self.qty         = sq
                self.entry_price = Decimal(
                    await self.ws.get_entry_price(self.inst, "short")
                )
                return

            await asyncio.sleep(0.1)

    async def _place_tp(self):
        # TP по percent из .env
        pct   = float(get("TP_SIZE", "1") or "1") / 100
        # считаем px
        if self.entry_price is None:
            raise ValueError("entry_price is None in _place_tp()")
        pct_decimal = Decimal(str(pct))
        if self.side == "long":
            tp_px = self.entry_price * (Decimal("1") + pct_decimal)
            action = "sell"
        else:
            tp_px = self.entry_price * (Decimal("1") - pct_decimal)
            action = "buy"

        tp_px = round(tp_px, 8)
        if self.side is None:
            raise ValueError("side is None in _place_tp()")
        cid   = f"P{self.side[0].upper()}{self.inst.replace('-', '')}"

        data = {
            "instId":       self.inst,
            "instType":     get("INST_TYPE", "SWAP"),
            "tdMode":       get("TD_MODE", "cross"),
            "ordType":      "conditional",
            "posSide":      self.side,
            "side":         action,
            "algoClOrdId":  cid,
            "tpTriggerPx":  str(tp_px),
            "tpOrdPx":      "-1",
            "closeFraction": "1",
            "reduceOnly":    True
        }

        logger.debug(f"🎯 [PreHEDGE] placing TP: {data}")
        try:
            await self.rest.request(
                "POST",
                "/api/v5/trade/order-algo",
                data=data
            )
            logger.info(f"🎯 [PreHEDGE] TP placed @ {tp_px}")
        except Exception as e:
            logger.error(f"❌ [PreHEDGE] TP placement failed: {e}", exc_info=e)

    def _on_ticker(self, last_px: float):
        # как только есть entry_price, считаем PnL
        if self.entry_price is None:
            return

        pnl = (Decimal(last_px) - self.entry_price) / self.entry_price
        # учёт шорта
        if self.side == "short":
            pnl = -pnl

        # если ниже −threshold → триггерим
        if pnl < -self.threshold:
            self._loss_evt.set()
