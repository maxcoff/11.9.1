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

        # Задержка для фильтрации всплесков
        self.spike_filter_delay = float(get("SPIKE_FILTER_DELAY", "0.0")or "0.0")
        # таск отложенной проверки
        self._loss_task: asyncio.Task | None = None

        self.tp_active     = False
        # Событие наступления loss-триггера
        self._loss_evt     = asyncio.Event()

        # Заполнятся в _wait_for_position
        self.entry_price: Decimal | None = None
        self.side:       str     | None = None  # "long" или "short"
        self.qty:        float   | None = None

        # Параметры для закрытия позиции
        self.td_mode = get("TD_MODE", "cross")
        self.order_sz = float(get("ORDER_SIZE", "0.01") or "0.01")


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
        logger.debug("[PreHEDGE] loss_task alive? %s",    self._loss_evt is not None and not self._loss_evt.is_set())
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
            #debug
            logger.debug(f"[PreHEDGE] waiting for position: long={lq:.4f}, short={sq:.4f},self.side={self.side}, self.qty={self.qty}, ")
            # ждем 100 мс

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

        #logger.debug(f"🎯 [PreHEDGE] placing TP: {data}")
        try:
            response = await self.rest.request(
                "POST",
                "/api/v5/trade/order-algo",
                data=data
            )
            msg = response.get("msg", "")
            code = int(response.get("code", -1))            
            if code == 0:
                logger.info(f"🎯 [PreHEDGE] TP placed @ {tp_px}")
                return
            if code in (51277, 51279):   # если не попадаем в цену (улетела), то продаем позицию
                logger.error(f"❌ [PreHEDGE] TP skip detected (code={code}, msg={msg}), closing position MARKET")            
                if code == 51277:
                    side, pos_side = "sell", "long"
                else:
                    side, pos_side = "buy",  "short"
                logger.error(f"placing IOC-close: side={side}, posSide={pos_side}, sz={self.order_sz}")
                try:
                    await self.rest.request(
                        "POST",
                        "/api/v5/trade/order",
                        data={
                            "instId":  self.inst,
                            "tdMode":  self.td_mode,
                            "ordType": "optimal_limit_ioc",
                            "posSide": pos_side,
                            "side":    side,
                            "sz":      self.order_sz})
                    logger.info(f"⚡️ [PreHEDGE] IOC-close placed: side={side}, posSide={pos_side}, sz={self.order_sz}")
                except Exception as e:
                    logger.error(f"❌ [PreHEDGE] IOC-close failed: {e}", exc_info=True)
                return
        except Exception as e:
            logger.error(f"❌ [PreHEDGE] TP placement failed: {e}", exc_info=e)

    def _on_ticker(self, last_px: float):
        # как только есть entry_price, считаем PnL
        if self.entry_price is None:
            return
        
        pnl = (Decimal(last_px) - self.entry_price) / self.entry_price
        #debug
        logger.debug(f"[PreHEDGE] ticker: last_px={last_px}, entry_price={self.entry_price}, pnl={pnl:.4f}")
        
        # учёт шорта
        if self.side == "short":
            pnl = -pnl

        # если ниже −threshold → триггерим
        
        if pnl < -self.threshold:
            logger.debug(f"[PreHEDGE] ticker: last_px={last_px}, entry_price={self.entry_price}, pnl={pnl:.4f}")
        
            if self.spike_filter_delay > 0:
                # запускаем только одну отложенную проверку
                if not self._loss_task or self._loss_task.done():
                    self._loss_task = asyncio.create_task(
                        self._delayed_loss_check()
                    )
                

            else:
                if not self._loss_evt.is_set():
                    logger.info(f"🚨 [PreHEDGE] loss trigger hit: pnl={pnl:.4f}")
                    # сразу триггерим
                    self._loss_evt.set()
                
    
    async def _delayed_loss_check(self):
        await asyncio.sleep(self.spike_filter_delay)        
        # получаем актуальную цену (WS или REST)
        raw_px = await self.ws.get_mark_price(self.inst)
        # теперь уже можно в Decimal       
        
        last_px = Decimal(str(raw_px))        
        #last_px = self.ws.get_last_price(self.inst)
        # или можно через REST
        # resp = await self.rest.request("GET", f"/market/ticker?instId={self.inst}")
        # last_px = float(resp["data"][0]["last"])
        if self.entry_price is None:  # 
            return
        assert isinstance(self.entry_price, Decimal)

        pnl = (Decimal(last_px) - self.entry_price) / self.entry_price
        #debug
        logger.debug(f"[PreHEDGE] delayed loss check: last_px={last_px}, entry_price={self.entry_price}, pnl={pnl:.4f}")    
        if self.side == "short":
            pnl = -pnl
        if pnl < -self.threshold:
            logger.info( f"⏱ [PREHEDGE] confirmed loss after delay: pnl={pnl:.4f}", extra={"mode":"HEDGE"} )
            self._loss_evt.set()
        else:
            logger.info( f"⏱ [PREHEDGE] spike filtered: pnl back to {pnl:.4f}", extra={"mode":"HEDGE"} )
        