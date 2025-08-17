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

        # ws подписка на позиции
        self.ws.subscribe(channel="positions", inst_id=self.inst, callback=self._on_ws_position)
        #self.ws.subscribe("positions", self.inst, self._on_ws_position)

        # рабочие переменные для хранения последнего состояния
        self._last_long  = (0.0, 0.0)
        self._last_short = (0.0, 0.0)
        self._cache: dict[str, tuple[float, float]] = {"long": (0.0, 0.0),
                                               "short": (0.0, 0.0)}

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
            # первый раз берём данные REST-ом если при старте чтото не получили
        await self._fetch_and_store()
            # подписываемся на WS-обновления позиций
        self.ws.subscribe(
            channel="positions",
            inst_id=self.inst,
            callback=self._on_ws_position
        )
            # фоновый «heartbeat» REST раз в 30 с
        async def _backup_loop():
            while True:
                try:
                    await asyncio.sleep(30)
                    await self.sync_from_rest()
                except asyncio.CancelledError:
                    raise

            #  запускаем heartbeat параллельно главному циклу orchestrator
        await _backup_loop()          # если orchestrator уже делает gather,
                                        # можно просто вернуть задачу
        

    # ----------------------------------------------------------

    async def _on_ws_position(self, msg: dict) -> None:
        # Обработчик WS-обновления позиций.        
        logger.debug("[WS-POS] raw message: %s", msg)
        for pos in msg.get("data", []):
            if pos.get("instId") != self.inst:
                continue
            side = pos.get("posSide")          # "long" или "short"
            qty  = float(pos.get("pos", 0))
            px   = float(pos.get("avgPx", 0))
            self._cache[side] = (qty, px)      # <-- кэшируем

            logger.debug("[WS-POS] parsed: %s %s qty=%s px=%s", self.inst, side, qty, px)

        # теперь у нас всегда свежие данные обеих ног без REST
        long_q, long_px   = self._cache["long"]
        short_q, short_px = self._cache["short"]

        # выбираем ту, что меньше
        if long_q == 0 and short_q == 0:
            chosen = None
        elif long_q < short_q or short_q == 0:
            chosen = ("long", long_q, long_px)
        else:  # short_q <= long_q
            chosen = ("short", short_q, short_px)

        async with self._lock:
            if chosen and chosen[2] > 0:
                self._snapshot = (Decimal(str(chosen[2])),
                                Decimal(str(chosen[1])),
                                chosen[0])
            else:
                self._snapshot = None
        logger.debug("[WS-POS] chosen snapshot=%s", self._snapshot)

    async def _fetch_and_store(self) -> None:
        """Читаем позиции и сохраняем данные только по «исходной» ноге."""
        long_q, long_px = await self._rest_position("long")
        short_q, short_px = await self._rest_position("short")        
        
        self._last_long  = (long_q, long_px)
        self._last_short = (short_q, short_px)
        
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
        
        # debug 
        logger.debug(f"[PositionSnapshot] updated: {self._snapshot}",extra={"mode": "SNAPSHOT"})
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