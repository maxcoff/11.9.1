import asyncio
import time
from typing import Optional, List, Dict

from core.config import get
from core.logger import logger


class OrderClient:
    def __init__(self, rest_client, ws_monitor, logger=logger):
        self.rest       = rest_client
        self.ws_monitor = ws_monitor
        self.logger     = logger

        # текущее состояние лимитников
        self.ord_ids: List[str] = []

    async def cancel_order(self, inst: str, ord_id: str):
        """Отменить один ордер."""
        try:
            await self.rest.request(
                "POST", "/api/v5/trade/cancel-order",
                data={"instId": inst, "ordId": ord_id}
            )
            self.logger.info(f"🛑 [OrderMode] cancelled {ord_id}", extra={"mode":"ORDER"})
        except Exception as e:
            self.logger.error(
                f"❌ [OrderMode] cancel-order failed for {ord_id}: {e}",
                extra={"mode":"ORDER"}, exc_info=e
            )

    async def cancel_all_orders(self, inst: str):
        """Снять все pending-ордера через POST cancel-order."""
        try:
            resp = await self.rest.request(
                "GET", "/api/v5/trade/orders-pending",
                params={"instId": inst, "instType": get("INST_TYPE", "SWAP")}
            )
            pending = [o["ordId"] for o in resp.get("data", []) if o.get("ordId")]
            if pending:
                self.logger.info(f"🧹 [OrderMode] cancelling pending: {pending}",
                                 extra={"mode":"ORDER"})
            for oid in pending:
                await self.cancel_order(inst, oid)
        except Exception as e:
            self.logger.warning(
                f"⚠️ [OrderMode] cancel-all failed: {e}",
                extra={"mode":"ORDER"}
            )

    async def run(self):
        """
        Запускаем цикл:
          1) ждем, пока нет открытых позиций
          2) чистим старые лимитники
          3) вычисляем mid±spread
          4) ставим пару FOK-лимитников
          5) ждём fill или таймаут → return
        """
        inst     = get("INSTRUMENT") or ""
        size     = float(get("ORDER_SIZE") or "0")
        spread   = float(get("SPREAD") or "0")
        interval = float(get("ORDER_INTERVAL") or "1")
        td_mode  = get("TD_MODE", "cross")

        self.logger.info("🔄 [OrderMode] starting order loop", extra={"mode":"ORDER"})

        # 0) Ждём, пока не закроются все позиции
        while True:
            lq = await self.ws_monitor.get_position(inst, "long")
            sq = await self.ws_monitor.get_position(inst, "short")
            if lq == 0 and sq == 0:
                break
            await asyncio.sleep(interval)

        # 1) снимаем все старые лимитники
        await self.cancel_all_orders(inst)

        # 2) достаём mid
        try:
            snap = await self.rest.fetch_snapshots(inst)
            bid  = float(snap["data"][0]["bids"][0][0])
            ask  = float(snap["data"][0]["asks"][0][0])
            mid  = (bid + ask) / 2
        except Exception as e:
            self.logger.error(f"❌ [OrderMode] book fetch failed: {e}", extra={"mode":"ORDER"})
            await asyncio.sleep(interval)
            return await self.run()

        buy_px  = round(mid * (1 - spread/2), 8)
        sell_px = round(mid * (1 + spread/2), 8)
        inst_clean = inst.replace("-", "")
        buy_clord  = "L" + inst_clean
        sell_clord = "S" + inst_clean

        # 3) выставляем два FOK-лимитника
        body_common = {
            "instId": inst,
            "tdMode": td_mode,
            "ordType": "limit",
            "tif":     "FOK",
            "sz":      str(size)
        }
        buy_body = {**body_common, "side":"buy", "posSide":"long", "px":str(buy_px), "clOrdId":buy_clord}
        sell_body= {**body_common, "side":"sell","posSide":"short","px":str(sell_px),"clOrdId":sell_clord}

        try:
            # параллельно постим оба ордера
            r_buy, r_sell = await asyncio.gather(
                self.rest.request("POST", "/api/v5/trade/order", data=buy_body),
                self.rest.request("POST", "/api/v5/trade/order", data=sell_body),
            )
            oid_buy  = r_buy["data"][0]["ordId"]
            oid_sell = r_sell["data"][0]["ordId"]
            self.ord_ids = [oid_buy, oid_sell]

            self.logger.info(
                f"🎯 [OrderMode] placed FOK: BUY {oid_buy}@{buy_px}, SELL {oid_sell}@{sell_px}",
                extra={"mode":"ORDER"}
            )
        except Exception as e:
            self.logger.error(f"❌ [OrderMode] place error: {e}", extra={"mode":"ORDER"})
            await asyncio.sleep(interval)
            return await self.run()

        # 4) ждём fill или таймаут
        filled = await self._wait_for_fill(timeout=interval)
        if not filled:
            self.logger.info(f"🕐 [OrderMode] no fill after {interval}s, retry", extra={"mode":"ORDER"})
            # чистим и перезапускаем цикл
            for oid in self.ord_ids:
                await self.cancel_order(inst, oid)
            return await self.run()

        # 5) post-fill cleanup
        await self._on_filled(filled, interval)
        self.logger.info(f"✅ [OrderMode] filled {filled}, switching to HEDGE", extra={"mode":"ORDER"})
        return

    async def _wait_for_fill(self, timeout: float) -> Optional[str]:
        """
        Ожидаем пока любая из ord_ids заполнится, или таймаут.
        Возвращает filled_ordId или None.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.2)
            for oid in self.ord_ids:
                status = await self.ws_monitor.get_order_status(oid)
                if status and status.lower().startswith("f"):
                    return oid
        return None

    async def _on_filled(self, filled: str, interval: float):
        """
        После fill-а:
          - отменяем вторую ножку
          - дожидаемся подтверждения отмены
          - чистим остатки через REST
        """
        inst = get("INSTRUMENT") or ""
        other = next(o for o in self.ord_ids if o != filled)

        # 1) отменить вторую
        await self.cancel_order(inst, other)

        # 2) дожидаемся WS-подтверждения отмены
        deadline = time.time() + interval
        while time.time() < deadline:
            st = await self.ws_monitor.get_order_status(other)
            if st and st.lower().startswith("c"):
                break
            await asyncio.sleep(0.1)

        # 3) чистим остатки через REST
        try:
            resp = await self.rest.request(
                "GET", "/api/v5/trade/orders-pending", params={"instId": inst}
            )
            pend = [o["ordId"] for o in resp.get("data", [])]
        except Exception:
            pend = []

        for oid in pend:
            if oid != filled:
                self.logger.info(f"🧹 [OrderMode] cleaning leftover {oid}", extra={"mode":"ORDER"})
                try:
                    await self.rest.request(
                        "POST","/api/v5/trade/cancel-order", data={"instId":inst,"ordId":oid}
                    )
                except Exception as e:
                    self.logger.error(
                        f"❌ [OrderMode] cancel leftover {oid} failed: {e}",
                        extra={"mode":"ORDER"}, exc_info=e
                    )

    async def place_order(
        self,
        side: str,
        order_type: str,
        qty: float,
        price: Optional[float] = None,
        pos_side: Optional[str]  = None
    ) -> Dict:
        """
        Универсальный метод для market/limit ордера.
        Возвращает полный dict из API или кидает.
        """
        inst    = get("INSTRUMENT") or ""
        td_mode = get("TD_MODE", "cross")
        data = {
            "instId":  inst,
            "tdMode":  td_mode,
            "ordType": order_type,
            "sz":      str(qty),
            "side":    side,
            "posSide": pos_side or ("long" if side == "buy" else "short")
        }
        if price is not None:
            data["px"] = str(price)

        resp = await self.rest.request("POST", "/api/v5/trade/order", data=data)
        data = resp.get("data", [])
        if not data:
            raise RuntimeError(f"Empty response on place_order: {resp}")
        return data[0]
