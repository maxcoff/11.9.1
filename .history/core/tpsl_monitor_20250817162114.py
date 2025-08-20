# tpsl_monitor.py

import asyncio
from typing import Optional, Tuple, List, Dict, Any
from decimal import Decimal
from contextlib import suppress

from core.config import get
from core.logger import logger


class TpslMonitor:
    def __init__(self, rest, ws_monitor,reinvest_manager):
        self.rest = rest
        self.ws = ws_monitor

        # Настройки инструмента
        self.inst = get("INSTRUMENT") or ""
        self.inst_type = get("INST_TYPE", "SWAP")
        self.td_mode = get("TD_MODE", "cross")

        self.reinvest_manager = reinvest_manager

        # Контекст сделки
        self._tp_lock = asyncio.Lock()
        self.entry_cid: str | None = None     # algoClOrdId для входа, который мы отследили
        self.entry_side: str = "long"         # "long"/"short" — сторона исходной ноги
        self.hedge_side: str | None = None    # если знаем явно; иначе вычислим как противоположную
        self.entry_price: Decimal = Decimal(0)
        self.entry_qty: Decimal = Decimal(0)

        # Событие фиксации TP
        self._pending_snapshot: Optional[Tuple[Decimal, Decimal, Decimal]] = None
        self.tp_filled_evt: asyncio.Event = asyncio.Event()
        self.last_tp_fill_px: float = 0.0

        # Параметры цикла
        self.interval = float(get("TPSL_WATCH_INTERVAL", "5") or 5)
        self.threshold = float(get("TPSL_PRICE_THRESH_PCT", "5") or 5) / 100

        # Параметры ретраев
        self.retry_delay = float(get("TPSL_RETRY_DELAY", "2") or 2)
        self.max_retries = int(get("TPSL_MAX_RETRIES", "0") or 0)  # 0 = бесконечно

        self.fetch_timeout = 5.0
        self._lock = asyncio.Lock()
        self._first_pass: bool = True

    def set_init_cid(self, cid: str) -> None:
        self._init_cid = cid

    def on_fill_event(self, ev: Any) -> None:
        cid = getattr(ev, "algoClOrdId", None)
        side = getattr(ev, "side", "").upper()
        fill_px = float(getattr(ev, "fillPx", 0) or 0)

        if cid != self.entry_cid or fill_px <= 0:
            return

        is_tp = (
            (self.entry_side == "long" and side == "SELL") or
            (self.entry_side == "short" and side == "BUY")
        )
        logger.debug(f"[TPSL-DEBUG] is_tp={is_tp} entry_side={self.entry_side} side={side}")
        if not is_tp:
            return

        self.last_tp_fill_px = fill_px        
        logger.info(f"[REINVEST-SIGNAL] tp_filled_evt.set() fired, fill_px={self.last_tp_fill_px}")

        self.tp_filled_evt.set()

    async def bootstrap(self) -> None:
        lq, lp, sq, sp = await self._get_state()
        if lq <= 0 or sq <= 0:
            return

        if lq < sq:
            init_side, init_px, init_qty = "long", lp, lq
            hedge_side, hedge_px, hedge_qty = "short", sp, sq
        else:
            init_side, init_px, init_qty = "short", sp, sq
            hedge_side, hedge_px, hedge_qty = "long", lp, lq

        tp_init, tp_hedge = self._calc_prices(
            init_side, init_px, init_qty,
            hedge_side, hedge_px, hedge_qty,
        )
        await self._reinstall_all(
            init_side, init_px, init_qty,
            hedge_side, hedge_px, hedge_qty,
            tp_init, tp_hedge
        )

    async def _loop(self):
        self.tp_filled_evt.clear()
        self._first_pass = True
        try:
            while True:
                await asyncio.sleep(self.interval)

                if self._first_pass:
                    self._first_pass = False
                    logger.debug("🔄 [TPSL] initial sync pass")
                    continue

                lq, lp, sq, sp = await self._get_state()
                if lp == 0 and sp == 0:
                    continue
                if lq <= 0 or sq <= 0:
                    continue

                if lq < sq:
                    init_side, init_px, init_qty = "long", lp, lq
                    hedge_side, hedge_px, hedge_qty = "short", sp, sq
                else:
                    init_side, init_px, init_qty = "short", sp, sq
                    hedge_side, hedge_px, hedge_qty = "long", lp, lq

                tp_init, tp_hedge = self._calc_prices(
                    init_side, init_px, init_qty,
                    hedge_side, hedge_px, hedge_qty
                )

                try:
                    algos = await asyncio.wait_for(
                        self._fetch_pending_algos(), timeout=self.fetch_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[TPSL] fetch_pending_algos timed out after {self.fetch_timeout}s",
                                   extra={"mode": "TPSL"})
                    continue

                inst_clean = self.inst.replace("-", "")
                cid_init = f"{init_side[0].upper()}{inst_clean}"
                cid_hedge = f"{hedge_side[0].upper()}{inst_clean}"

                current = {
                    o["algoClOrdId"]: float(o.get("tpTriggerPx") or 0)
                    for o in algos
                    if o.get("algoClOrdId") in (cid_init, cid_hedge)
                }

                mismatch = (
                    abs(current.get(cid_init, 0) - tp_init) / tp_init > self.threshold
                    or
                    abs(current.get(cid_hedge, 0) - tp_hedge) / tp_hedge > self.threshold
                )

                if mismatch:
                    async with self._lock:
                        logger.info("🛠️ [TPSL] mismatch detected → reinstall all algos")
                        await self._reinstall_all(
                            init_side, init_px, init_qty,
                            hedge_side, hedge_px, hedge_qty,
                            tp_init, tp_hedge
                        )

        except asyncio.CancelledError:
            logger.info("[TPSL] loop cancelled")
        except Exception as e:
            logger.error(f"❌ [TPSL] loop error: {e}", exc_info=e)

    async def _get_state(self) -> Tuple[float, float, float, float]:
        lq = await self.ws.get_position(self.inst, "long")
        sq = await self.ws.get_position(self.inst, "short")
        lp = sp = 0.0
        resp = await self.rest.request(
            "GET", "/api/v5/account/positions",
            params={"instId": self.inst}
        )
        for p in resp.get("data", []):
            if p["instId"] == self.inst:
                if p["posSide"] == "long":
                    lp = float(p.get("avgPx") or 0)
                elif p["posSide"] == "short":
                    sp = float(p.get("avgPx") or 0)
        return lq, lp, sq, sp

    async def _fetch_pending_algos(self) -> List[Dict[str, Any]]:
        tasks = [
            self.rest.request(
                "GET", "/api/v5/trade/orders-algo-pending",
                params={"instType": self.inst_type, "instId": self.inst, "ordType": t}
            )
            for t in ("oco", "conditional")
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        algos: List[Dict[str, Any]] = []
        for res in results:
            if isinstance(res, Exception):
                logger.warning(f"[TPSL] fetch pending failed: {res}")
            elif isinstance(res, dict):
                algos.extend(res.get("data", []))
            else:
                logger.warning(f"[TPSL] unexpected response type {type(res)}, skipping")
        return algos

    async def _cancel_all(self):
        try:
            algos = await asyncio.wait_for(
                self._fetch_pending_algos(), timeout=self.fetch_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"[TPSL] cancel-all fetch timeout {self.fetch_timeout}s",
                           extra={"mode": "TPSL"})
            algos = []

        to_cancel = [
            {"instId": self.inst, "algoId": a["algoId"]}
            for a in algos if a.get("algoId")
        ]
        if not to_cancel:
            return
        try:
            await self.rest.request("POST", "/api/v5/trade/cancel-algos", data=to_cancel)
            logger.info(f"🧹 [TPSL] cancelled algos: {to_cancel}")
        except Exception as e:
            logger.warning(f"[TPSL] cancel-all ignored: {e}")

    async def _reinstall_all(
        self,
        init_side: str, init_px: float, init_qty: float,
        hedge_side: str, hedge_px: float, hedge_qty: float,
        tp_init: float, tp_hedge: float
    ):
        logger.debug(
            f"🔄 [TPSL] reinstall_all → "
            f"init({init_side})={init_qty}@{init_px}, "
            f"hedge({hedge_side})={hedge_qty}@{hedge_px}, "
            f"tp_init={tp_init}, tp_hedge={tp_hedge}"
        )

        # 1) Отменяем старые условники
        await self._cancel_all()

        inst_clean = self.inst.replace("-", "")
        init_cid = f"{init_side[0].upper()}{inst_clean}"
        self.entry_cid = init_cid
        self.entry_side = init_side
        hedge_cid = f"{hedge_side[0].upper()}{inst_clean}"

        # 2) TPSL-OCO на исходную ногу
        oco = {
            "instId": self.inst,
            "instType": self.inst_type,
            "tdMode": self.td_mode,
            "ordType": "oco",
            "posSide": init_side,
            "side": "sell" if init_side == "long" else "buy",
            "algoClOrdId": init_cid,
            "tpTriggerPx": str(tp_init),
            "tpOrdPx": "-1",
            "slTriggerPx": str(tp_hedge),
            "slOrdPx": "-1",
            "closeFraction": 1,
            "reduceOnly": True
        }
        try:
            await self.rest.request("POST", "/api/v5/trade/order-algo", data=oco)
            logger.info(f"🎯 [TPSL] placed OCO({init_cid}) @ {tp_init}")
        except Exception as e:
            logger.error(f"❌ [TPSL] OCO(init) failed for {init_cid}: {e}")

        # 3) TP-Conditional на хедж-ногу
        cond = {
            "instId": self.inst,
            "instType": self.inst_type,
            "tdMode": self.td_mode,
            "ordType": "conditional",
            "posSide": hedge_side,
            "side": "sell" if hedge_side == "long" else "buy",
            "algoClOrdId": hedge_cid,
            "tpTriggerPx": str(tp_hedge),
            "tpOrdPx": "-1",
            "closeFraction": 1,
            "reduceOnly": True
        }

        attempt = 0
        while True:
            attempt += 1
            try:
                await self.rest.request("POST", "/api/v5/trade/order-algo", data=cond)
                logger.info(f"🎯 [TPSL] placed Conditional({hedge_cid}) @ {tp_hedge}")
                break
            except Exception as e:
                raw = e.args[0] if e.args else {}
                data = raw.get("data", []) if isinstance(raw, dict) else []
                codes = {d.get("sCode") for d in data if isinstance(d, dict)}

                # retry на коды 51279/51277
                if codes & {"51279", "51277"}:
                    logger.warning(
                        f"🔁 [TPSL] Conditional {hedge_cid} got sCode={codes}, "
                        f"retry in {self.retry_delay}s (#{attempt})"
                    )
                    if self.max_retries and attempt >= self.max_retries:
                        logger.error(f"[TPSL] max_retries={self.max_retries} reached, give up")
                        break
                    await asyncio.sleep(self.retry_delay)
                    continue

                logger.error(f"❌ [TPSL] conditional failed for {hedge_cid}: {e}")
                break

    def _calc_prices(
        self,
        init_side: str, init_px: float, init_qty: float,
        hedge_side: str, hedge_px: float, hedge_qty: float
    ) -> Tuple[float, float]:
        tp_pct = float(get("TP_SIZE", "1") or 1) / 100
        fee = float(get("FEE", "0") or 0)
        Ep = float(get("EP", "0") or 0)
        qty = float(get("ORDER_SIZE", "0") or (init_qty + hedge_qty))

        # tp_init
        if init_side == "long":
            tp_init = init_px * (1 + tp_pct)
        else:
            tp_init = init_px * (1 - tp_pct)
        tp_init = round(tp_init, 8)

        # breakeven для hedge
        if init_side == "long":
            long_px, long_qty = init_px, init_qty
            short_px, short_qty = hedge_px, hedge_qty
        else:
            short_px, short_qty = init_px, init_qty
            long_px, long_qty = hedge_px, hedge_qty

        adj = round(Ep * (1 + (long_qty + short_qty) / qty / 100 / 10), 8)
        num = long_qty * long_px * (1 + fee) - short_qty * short_px * (1 - fee)
        den = long_qty * (1 - fee) - short_qty * (1 + fee) - adj

        tp_hedge = round(num / den, 8)
        return tp_init, tp_hedge
