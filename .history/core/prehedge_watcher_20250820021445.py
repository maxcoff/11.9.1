import asyncio
import json
from decimal import Decimal
import inspect

from core.config import get
from core.logger import logger
#from core.task_manager import create_tracked_task

class PreHedgeWatcher:
    def __init__(self, rest, ws_monitor, hedge_manager):
        self.rest          = rest
        self.ws            = ws_monitor
        self.hedge_manager = hedge_manager

        self.inst          = get("INSTRUMENT", "") or ""
        self.inst_id          = get("INSTR", "") or ""
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

        self.best_bid_dec = None
        self.best_ask_dec = None
        self.mark_price_dec = None

        # Параметры для закрытия позиции
        self.td_mode = get("TD_MODE", "cross")
        self.order_sz = float(get("ORDER_SIZE", "0.01") or "0.01")

        self.tp_placed = False
        # где-то в init подписываемся
        self.last_trade_px = None
        self.ws.subscribe(channel="trades",inst_id=self.inst_id,callback=self._on_trade)
        


    def _on_trade(self, msg: dict):
        # msg['data'] — список сделок, берём первую или последнюю
        rec = msg.get("data", [{}])[0]
        px  = rec.get("px")
        print (f"debug111{rec}")
        if px is not None:
            try:
                self.last_trade_px = float(px)
                logger.debug(f"[TRADE] last_trade_px={self.last_trade_px}")
            except ValueError:
                pass

    

    async def run(self):    
        self.tp_placed = False # сброс флага    
        self.tp_active = False # сброс флага
        
        # сброс перед запуском
        #if not hasattr(self, "_loss_evt") or self._loss_evt is None:
        #    self._loss_evt = asyncio.Event()
        #else:
        #    self._loss_evt.clear()

        
        #debug
        logger.info(f"🔄 [PreHEDGE] новый запуск tp_watcher")
        # 1) Подписываемся на тикеры
        self.ws.add_ticker_listener(self._on_ticker)
        

        # 2) Ждём, пока появится ровно одна нога позы
        await self._wait_for_position()
        logger.debug(f"[PreHEDGE] after _wait_for_position: entry_price={self.entry_price}, side={self.side}, qty={self.qty}")


        # 3) Сразу выставляем TP, если ещё не выставлен
        if not self.tp_active:
            # debug
            logger.info(f"🎯 [PreHEDGE] заходим в not self.tp_active ")
            await self._place_tp()
            self.tp_active = True

        # 4) Ждём loss-триггер
        logger.info(
            f"⏳ [PreHEDGE] waiting for loss ≤ −{self.threshold*100:.2f}%"
        )
        #logger.debug("[PreHEDGE] loss_task alive? %s",    self._loss_evt is not None and not self._loss_evt.is_set())
        logger.debug("[PreHEDGE]_loss_task alive? %s",   self._loss_task is not None and not self._loss_task.done())
        
        await self._loss_evt.wait()        
        logger.info("🚨 [PreHEDGE] loss trigger hit → запускаю hedge")        
        await self.hedge_manager.run()
        # 5) ws отписка нах!
        self.ws.remove_ticker_listener(self._on_ticker)

    async def _wait_for_position(self):
        while True:
            lq = await self.ws.get_position(self.inst, "long")
            sq = await self.ws.get_position(self.inst, "short")

            if lq == 0 and sq == 0:
                logger.info("[PREHEDGE] позиций нет → выход")   
                return
            
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
            #logger.debug(f"[PreHEDGE] waiting for position: long={lq:.4f}, short={sq:.4f},self.side={self.side}, self.qty={self.qty}, ")
            # ждем 100 мс

            await asyncio.sleep(0.5)

    async def _place_tp(self):
        # сначала чистим то что там есть
        # 1. берём algoId последнего conditional-ордера, который висит на хедже
        pending = await self.rest.request(
            "GET",
            "/api/v5/trade/orders-algo-pending",
            params={"instId": self.inst, "ordType": "conditional"}
        )        
        conditional = next((o for o in pending.get("data", [])), None)

        if conditional:            
            algo_id = conditional["algoId"]
            print (f"[PreHEDGE] cancel-algos: algoId={algo_id}")
            try:
                to_cancel = [{"instId": self.inst, "algoId": algo_id}]
                await self.rest.request(
                    "POST",
                    "/api/v5/trade/cancel-algos", data=to_cancel)                    
            except Exception as e:
                if getattr(e, "code", 0) == 51000:
                    logger.debug("[PreHEDGE] conditional уже отменён")
                else:
                    logger.warning(f"[PreHEDGE] cancel failed: {e}")
        else:
            logger.debug("[PreHEDGE] этот TP не conditional либо его вообще нет => пропускаем отмену")
        # TP по percent из .env
        pct   = float(get("TP_SIZE", "1") or "1") / 100
        
        if self.tp_placed:
            return  
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

        try:
            response = await self.rest.request(
                "POST",
                "/api/v5/trade/order-algo",
                data=data
            )
            #logger.debug(f"🎯 [PreHEDGE] placing TP: {data}")

            self.tp_placed = True
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
                    await self.rest.request(         # правда существует OrderClient.place_order, но тут я про него забыл ну и обработка ответа уже написана и пох
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
            if code not in (0, 51277, 51279):
                logger.error(f"❌ [PreHEDGE] TP placement failed: code={code}, msg={msg}")
                return
        except Exception as e:
            logger.error(f"❌ [PreHEDGE] TP placement failed: {e}", exc_info=e)

    def _on_ticker(self, last_px: float):
        # хедж уже сработал            — выходим
        if self._loss_evt.is_set():
            return  
        # нет входной цены или стороны — выходим
        if self.entry_price is None or self.side is None:
            return
        
        mark_px = self.ws.get_mark_price(self.inst)

        #last_px_dec = Decimal(str(last_px))
        entry_dec   = Decimal(str(self.entry_price))
        threshold_dec = Decimal(str(self.threshold))  # положительное число

        # +1 для long, -1 для short
        direction = Decimal("1") if self.side == "long" else Decimal("-1")

        price_for_pnl = Decimal(str(mark_px))  # 
        pnl = direction * (Decimal(str(price_for_pnl)) - entry_dec) / entry_dec

        
        #logger.debug(
        #    "[PNL DEBUG] side=%s | entry_price=%s | mark_px=%s  "
        #    "| direction=%s |  pnl=%s | threshold=%s",
        #    self.side,
        #    self.entry_price,
        #    mark_px,            
        #    direction,            
        #    pnl,
        #    threshold_dec
        #    )

        logger.debug(
            f"[PreHEDGE] PnL={pnl:.4f}, порог={threshold_dec:.4f}, событие_убытка={self._loss_evt.is_set()}"    )

        # триггер по убытку
        if pnl < -threshold_dec:
            if self.spike_filter_delay > 0:
                # создаём только одну отложенную проверку
                if not self._loss_task or self._loss_task.done():
                    logger.debug("[PreHEDGE] создаю _loss_task из %s", inspect.stack()[1].function)
                    self._loss_task = asyncio.create_task(self._delayed_loss_check())
            else:
                if not self._loss_evt.is_set():
                    logger.info(f"🚨 [PreHEDGE] Достигнут порог убытка: pnl={pnl:.4f} (threshold={threshold_dec:.4f})")
                    self._loss_evt.set ()
                    logger.debug(f"🚨 [PreHEDGE] событие_убытка={self._loss_evt.is_set()}"    )
                
    
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
            #self._loss_evt = None
        else:
            logger.info( f"⏱ [PREHEDGE] spike filtered: pnl back to {pnl:.4f}", extra={"mode":"HEDGE"} )
        