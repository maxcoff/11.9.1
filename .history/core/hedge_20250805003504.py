# hedge.py

import asyncio
from typing import Tuple

from core.config import get
from core.config import get_float
from core.logger import logger


class HedgeManager:
    def __init__(self, rest_client, ws_monitor, logger=logger):
        self.rest       = rest_client
        self.ws         = ws_monitor
        self.logger     = logger
        self.inst       = get("INSTRUMENT") or ""
        self.td_mode    = get("TD_MODE", "cross")

        # параметры из окружения
        self.hedge_cf   = float(get("HEDGE_CF",          "1.0") or 1.0)
        self.timeout    = float(get("HEDGE_TIMEOUT",     "10")  or 10.0)
        self.poll_delay = float(get("HEDGE_POLL_DELAY",  "0.5") or 0.5)
        self.spike_filter_delay = float(get("SPIKE_FILTER_DELAY", "0.0")or "0.0")

    async def run(self) -> None:
        """
        1) Если обе ноги есть → skip.
        2) Ждём открытия первой (initial) ноги.
        3) Считаем размер и делаем market‐хедж.
        4) Ждём подтверждения fill (WS или REST) до timeout.
        """
        # 1) стартовая проверка WS-позиций
        ws_long, ws_short = await self._get_ws_positions()
        self.logger.info(
            f"▶️ [HEDGE] run start: WS pos (L,S)=({ws_long:.4f},{ws_short:.4f})",
            extra={"mode":"HEDGE"}
        )

        # если обе ноги уже открыты — ничего не делаем
        if ws_long > 0 and ws_short > 0:
            self.logger.info(
                "ℹ️ [HEDGE] both legs exist → skipping hedge",
                extra={"mode":"HEDGE"}
            )
            return

        # 2) ждём появления первой ноги (initial)
        initial_pos = ws_long - ws_short  # long>0 → positive, short>0 → negative
        
        ''' # не понятно зачем ждать ногу, если тебя запустили с ноги
        if initial_pos == 0:
            self.logger.info(
                "⏳ [HEDGE] waiting for initial leg",
                extra={"mode":"HEDGE"}
            )
            while True:
                await asyncio.sleep(self.poll_delay)
                ws_long, ws_short = await self._get_ws_positions()
                #self.logger.debug( f"[HEDGE] poll WS pos (L,S)=({ws_long:.4f},{ws_short:.4f})", extra={"mode":"HEDGE"} )
                if ws_long > 0 or ws_short > 0:
                    initial_pos = ws_long - ws_short
                    break
        self.logger.info(f"🔍 [HEDGE] initial position detected: {initial_pos:.4f}", extra={"mode":"HEDGE"})'''

        # «ANTI-SPIKE» ЗАДЕРЖКА
        if self.spike_filter_delay > 0:
            self.logger.info(f"⏱ [HEDGE] spike-filter: waiting {self.spike_filter_delay:.2f}s", extra={"mode":"HEDGE"})
            await asyncio.sleep(self.spike_filter_delay)

        # 3) считаем размер хеджа и отправляем market‐order
        hedge_size = abs(initial_pos) * self.hedge_cf
        side       = "sell" if initial_pos > 0 else "buy"
        pos_side   = "short" if initial_pos > 0 else "long"

        try:
            ws_long, ws_short = await self._get_ws_positions()            
            if ws_long == 0 and ws_short == 0:
                self.logger.warning("[HEDGE] ups, first leg lost → skipping hedge",extra={"mode":"HEDGE"})                
                return

            self.logger.info(
                f"🔧 [HEDGE] placing market hedge: side={side}, size={hedge_size:.4f}",extra={"mode":"HEDGE"})
        
            resp = await self.rest.request(
                "POST", "/api/v5/trade/order",
                data={
                    "instId":  self.inst,
                    "tdMode":  self.td_mode,
                    "side":    side,
                    "posSide": pos_side,
                    "ordType": "market",
                    "sz":      str(hedge_size)
                }
            )
            hedge_ord = resp["data"][0]["ordId"]
            self.logger.info(f"🎯 [HEDGE] placed hedge ordId={hedge_ord}",extra={"mode":"HEDGE"})
        except Exception as e:
            self.logger.error(
                f"❌ [HEDGE] place hedge failed: {e}",
                extra={"mode":"HEDGE"}, exc_info=e
            )
            return

        # 4) ждём подтверждения fill (WS или REST) до timeout
        deadline = asyncio.get_event_loop().time() + self.timeout
        self.logger.debug(
            f"[HEDGE] waiting for fill up to +{self.timeout}s",
            extra={"mode":"HEDGE"}
        )
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(self.poll_delay)

            ws_long, ws_short = await self._get_ws_positions()
            rest_long, rest_short = await self._get_rest_positions()
            self.logger.debug(
                f"[HEDGE] WS(L,S)=({ws_long:.4f},{ws_short:.4f}), "
                f"REST(L,S)=({rest_long:.4f},{rest_short:.4f})",
                extra={"mode":"HEDGE"}
            )

            # если обе ноги >0 на любой стороне — считается fill
            if (ws_long > 0 and ws_short > 0) or (rest_long > 0 and rest_short > 0):
                self.logger.info(
                    "✅ [HEDGE] hedge confirmed",
                    extra={"mode":"HEDGE"}
                )
                break
        else:
            self.logger.warning(
                f"⚠️ [HEDGE] fill timeout after {self.timeout}s",
                extra={"mode":"HEDGE"}
            )

        self.logger.info(
            "→ HEDGE.run() done, идем в TPSL",
            extra={"mode":"HEDGE"}
        )


    async def _get_ws_positions(self) -> Tuple[float, float]:
        """Возвращает (long_qty, short_qty) из WS."""
        lq = await self.ws.get_position(self.inst, "long")
        sq = await self.ws.get_position(self.inst, "short")
        return lq, sq

    async def _get_rest_positions(self) -> Tuple[float, float]:
        """Возвращает (long_qty, short_qty) через REST."""
        resp = await self.rest.request(
            "GET", "/api/v5/account/positions",
            params={"instId": self.inst}
        )
        long_r = short_r = 0.0
        for p in resp.get("data", []):
            if p.get("instId") == self.inst:
                if p.get("posSide") == "long":
                    long_r = float(p.get("pos") or 0)
                elif p.get("posSide") == "short":
                    short_r = float(p.get("pos") or 0)
        return long_r, short_r
