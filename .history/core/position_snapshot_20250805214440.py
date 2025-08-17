# core/position_snapshot.py
import asyncio
from decimal import Decimal
from typing import Optional, Tuple

from core.config import get
from core.logger import logger


class PositionSnapshot:
    """
    Хранит «исходную» ногу (entry_price, entry_qty, side) и
    обновляет её при каждом появлении новой позиции.
    """

    def __init__(self, rest, ws_monitor):
        self.rest = rest
        self.ws   = ws_monitor
        self.inst = get("INSTRUMENT") or ""

        # Актуальные данные
        self._snapshot: Optional[Tuple[Decimal, Decimal, str]] = None  # (ep, eq, side)
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------
    async def get(self) -> Optional[Tuple[Decimal, Decimal, str]]:
        """Возвращает последний актуальный снимок или None."""
        async with self._lock:
            return self._snapshot

    async def sync_from_rest(self) -> Optional[Tuple[Decimal, Decimal, str]]:
        await self._fetch_and_store()
        async with self._lock:
            return self._snapshot

    # ----------------------------------------------------------
    # Внутренний цикл обновления
    # ----------------------------------------------------------
    async def run(self) -> None:
        """Корутина, которую запускают разово из orchestrator.run()."""
        try:
            while True:
                await self._fetch_and_store()
                await asyncio.sleep(1)  # достаточно 1 Гц
        except asyncio.CancelledError:
            #logger.info("[PositionSnapshot] run cancelled", extra={"mode": "SNAPSHOT"}) 
            raise

    # ----------------------------------------------------------
    async def _fetch_and_store(self) -> None:
        """Читаем позиции и сохраняем данные только по «исходной» ноге."""
        long_q, long_px = await self._rest_position("long")
        short_q, short_px = await self._rest_position("short")
        
        init_side = None
        init_qty = 0.0
        init_px = 0.0
        if long_q > 0 or short_q > 0:
            if long_q < short_q:
                init_side, init_qty, init_px = "long", long_q, long_px
            elif short_q < long_q:
                init_side, init_qty, init_px = "short", short_q, short_px
            else: # qty равны – считаем, что позиции уже закрыты
                init_side = None
        
        async with self._lock:
            if init_side and init_px > 0:                
                self._snapshot = (Decimal(str(init_px)),
                                  Decimal(str(init_qty)),
                                  init_side)
            else:
                self._snapshot = None
                
    # ----------------------------------------------------------
    async def _rest_position(self, side: str) -> Tuple[float, float]:
        """qty, avgPx по REST."""
        resp = await self.rest.request(
            "GET", "/api/v5/account/positions", params={"instId": self.inst}
        )
        for p in resp.get("data", []):
            if p.get("posSide") == side:
                return float(p.get("pos", 0)or 0), float(p.get("avgPx", 0) or 0)
        return 0.0, 0.0