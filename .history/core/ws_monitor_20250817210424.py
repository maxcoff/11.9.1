import asyncio
import aiohttp
import hmac
import hashlib
import base64
import time

from typing import Optional, Dict, Tuple, Callable, List, Any, Awaitable, Coroutine, Iterable
from types import SimpleNamespace
from dataclasses import dataclass

from core.config import get
from core.logger import logger

@dataclass
class AlgoFillEvent:
    # Идентификаторы
    algoClOrdId: str | None
    clOrdId: str | None
    ordId: str | None
    # Стороны
    side: str                 # "BUY" / "SELL"
    posSide: str | None       # "long" / "short" (если биржа даёт)
    # Исполнение
    fillPx: float             # цена исполнения
    fillSz: float             # выполненный объём (за это событие)
    accFillSz: float | None   # кумулятивный объём (если есть)
    # Тип/категория
    ordType: str | None       # например, "oco", "limit", "market"
    is_oco: bool              # наш флаг OCO (см. нормализацию)
    ts: int | None            # отметка времени (мс)

class WSMonitor:
    """
    WebSocket monitor that maintains:
      - private WS (orders, positions)
      - public WS  (tickers)
    Allows listeners to subscribe to ticker updates and algo-order fill events.
    """

    def __init__(
        self,
        rest_client,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        ws_url: Optional[str] = None,             # legacy alias
        ws_url_private: Optional[str] = None,      # preferred        
    ) -> None:
        
        self.rest_client   = rest_client

        # private WS URL
        default_priv        = "wss://ws.okx.com:8443/ws/v5/private"
        self.ws_url_private = (
            ws_url
            or ws_url_private
            or get("WS_URL", default_priv)
            or default_priv
        )

        # public WS URL
        default_pub         = "wss://ws.okx.com:8443/ws/v5/public"
        self.ws_url_public  = get("WS_URL_PUBLIC", default_pub) or default_pub

        self.api_key        = api_key
        self.api_secret     = api_secret
        self.passphrase     = passphrase

        # instrument settings
        self.inst           = get("INSTRUMENT", "") or ""
        self.inst_type      = get("INST_TYPE", "SWAP") or "SWAP"

        # state
        self._lock          = asyncio.Lock()
        self.positions: Dict[Tuple[str, str], float]    = {}
        self.entry_prices: Dict[Tuple[str, str], float] = {}
        self.orders: Dict[str, str]                     = {}
        self._last_mark_px  = 0.0
        self.latest_price: dict[str, float] = {}

        # backoff for reconnect
        self._backoff_priv  = 1
        self._backoff_pub   = 1
        self._max_backoff   = 60

        # sessions & tasks
        self._session_priv: Optional[aiohttp.ClientSession] = None
        self._session_pub:  Optional[aiohttp.ClientSession] = None
        self._task_priv:    Optional[asyncio.Task]         = None
        self._task_pub:     Optional[asyncio.Task]         = None

        self._running       = False

        # listeners
        self._ticker_listeners: List[Callable[[float], None]] = []
        #self._algo_listeners:   List[Callable[[Any], None]]   = []
        self._algo_listeners: list[Callable[[AlgoFillEvent], Any]] = []
        self._ext_listeners: dict[tuple[str, str], Callable[[dict], Awaitable[None]]] = {}

    async def connect(self) -> None:
        """Start private and public WS tasks."""
        if not self._running:
            self._running    = True
            self._task_priv  = asyncio.create_task(self._run_private())
            self._task_pub   = asyncio.create_task(self._run_public())

    async def disconnect(self) -> None:
        """Stop both WS tasks and close sessions."""
        self._running = False

        if self._task_priv:
            self._task_priv.cancel()
            try:
                await self._task_priv
            except asyncio.CancelledError:
                pass
            self._task_priv = None

        if self._task_pub:
            self._task_pub.cancel()
            try:
                await self._task_pub
            except asyncio.CancelledError:
                pass
            self._task_pub = None

        if self._session_priv:
            await self._session_priv.close()
            self._session_priv = None

        if self._session_pub:
            await self._session_pub.close()
            self._session_pub = None

    # -- Private WS: orders, positions --

    async def _run_private(self) -> None:
        try:
            while self._running:
                try:
                    if not self._session_priv:
                        self._session_priv = aiohttp.ClientSession()

                    async with self._session_priv.ws_connect(self.ws_url_private) as ws:
                        await self._login(ws)
                        await self._subscribe(ws, ["orders", "positions"], public=False)
                        self._backoff_priv = 1

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(msg.json())
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                raise RuntimeError("WS-PRIV error")
                except Exception as e:
                    logger.error(
                        f"🔄 [WS-PRIV] Disconnected: {e}, retry in {self._backoff_priv}s",
                        extra={"mode": "WS"}
                    )
                    await asyncio.sleep(self._backoff_priv)
                    self._backoff_priv = min(self._backoff_priv * 2, self._max_backoff)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        finally:
            if self._session_priv:
                await self._session_priv.close()
                self._session_priv = None

    async def _login(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        ts      = str(time.time())
        to_sign = ts + "GET" + "/users/self/verify"
        sign    = base64.b64encode(
            hmac.new(self.api_secret.encode(),
                     to_sign.encode(),
                     hashlib.sha256).digest()
        ).decode()

        req = {
            "op":   "login",
            "args": [{
                "apiKey":     self.api_key,
                "passphrase": self.passphrase,
                "timestamp":  ts,
                "sign":       sign
            }]
        }
        await ws.send_json(req)
        resp = await ws.receive_json(timeout=5)
        if str(resp.get("code")) != "0":
            raise RuntimeError(f"WS login failed: {resp}")

        logger.info("✅ [WS-PRIV] login successful", extra={"mode": "WS"})

    async def _subscribe(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        channels: List[str],
        public: bool
    ) -> None:
        for ch in channels:
            args = {"channel": ch}
            if not public:
                args.update({"instType": self.inst_type, "instId": self.inst})
            else:
                args["instId"] = self.inst

            req = {"op": "subscribe", "args": [args]}
            await ws.send_json(req)

            # wait confirmation
            while True:
                msg = await ws.receive_json(timeout=5)
                evt = msg.get("event")
                arg = msg.get("arg", {})
                if evt == "subscribe" and arg.get("channel") == ch:
                    tag = "WS-PUB" if public else "WS-PRIV"
                    logger.info(f"✅ [{tag}] subscribed to {ch}", extra={"mode":"WS"})
                    break
                if evt == "error":
                    raise RuntimeError(f"WS subscribe {ch} failed: {msg}")

    async def _handle_message(self, msg: dict) -> None:
        ch   = msg.get("arg", {}).get("channel")
        data = msg.get("data", [])

        # внутренняя обработка
        if ch == "orders":
            await self._process_orders(data)
        elif ch == "positions":
            await self._process_positions(data)

        # внешние подписчики
        inst_id = msg.get("arg", {}).get("instId")
        key = (ch, inst_id)
        listener = self._ext_listeners.get(key)
        if listener is not None:
            try:
                coro: Coroutine[None, None, None] = listener(msg) # type: ignore
                asyncio.create_task(coro)
            except Exception:
                logger.exception("Error in external listener", extra={"mode":"WS"})

    async def _process_orders(self, data: list) -> None:
        async with self._lock:
            for o in data:
                ord_id = o.get("ordId")
                state  = o.get("state")
                if ord_id and state:
                    self.orders[ord_id] = state

                # log any fill
                if state == "filled" and o.get("fillSz") and o.get("fillPx"):
                    logger.info(
                        f"⚡ [FILL] {o['instId']}: "
                        f"{o['side'].upper()}/{o['posSide']} "
                        f"closed size={o['fillSz']}@{o['fillPx']}",
                        extra={"mode":"FILL"}
                    )

                # catch algo-order fills by presence of algoClOrdId
                algo_cid = o.get("algoClOrdId")
                if algo_cid and state == "filled":
                    evt = SimpleNamespace(**o)
                    for fn in self._algo_listeners:
                        try:
                            fn(evt) # type: ignore #
                        except Exception:
                            logger.exception(
                                "Error in algo-listener", extra={"mode":"WS"}
                            )

    async def _process_positions(self, data: list) -> None:
        async with self._lock:
            for itm in data:
                inst = itm.get("instId")
                side = itm.get("posSide")
                pos  = float(itm.get("pos")   or 0)
                avg  = float(itm.get("avgPx") or 0)
                self.positions[(inst, side)]    = pos
                self.entry_prices[(inst, side)] = avg

    # -- Public WS: tickers --

    async def _run_public(self) -> None:
        try:
            while self._running:
                try:
                    if not self._session_pub:
                        self._session_pub = aiohttp.ClientSession()

                    async with self._session_pub.ws_connect(self.ws_url_public) as ws:
                        await self._subscribe(ws, ["tickers"], public=True)
                        self._backoff_pub = 1

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = msg.json()
                                ch   = data.get("arg", {}).get("channel")
                                if ch == "tickers":
                                    self._process_tickers(data.get("data", []))
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                raise RuntimeError("WS-PUB error")
                except Exception as e:
                    logger.error(
                        f"🔄 [WS-PUB] Disconnected: {e}, retry in {self._backoff_pub}s",
                        extra={"mode": "WS"}
                    )
                    await asyncio.sleep(self._backoff_pub)
                    self._backoff_pub = min(self._backoff_pub * 2, self._max_backoff)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        finally:
            if self._session_pub:
                await self._session_pub.close()
                self._session_pub = None


    def _process_tickers(self, data: list) -> None:
        if not data or not isinstance(data, list):
            return
        rec = data[0]
        last = rec.get("last")
        if last is None:
            return
        try:
            price = float(last)
        except (TypeError, ValueError):
            return

        self._last_mark_px = price
        for fn in self._ticker_listeners:
            try:
                fn(price)
            except Exception:
                logger.exception("Error in ticker listener", extra={"mode":"WS"})

    # -- Public API for listeners --

    def add_ticker_listener(self, fn: Callable[[float], None]) -> None:
        """
        Subscribe to real-time ticker (last price) updates.
        """
        self._ticker_listeners.append(fn)

    def add_algo_listener(self, fn: Callable[[Any], None]) -> None:
        """
        Subscribe to algorithmic order fill events.
        Passes a SimpleNamespace with attributes from the fill message.
        """
        self._algo_listeners.append(fn)
    
    def subscribe(self,
              channel: str,
              inst_id: str,
              callback: Callable[[dict], Awaitable[None]]) -> None:
        self._ext_listeners[(channel, inst_id)] = callback

    # -- Getters --

    async def get_position(self, inst: str, side: str) -> float:
        async with self._lock:
            return self.positions.get((inst, side), 0.0)

    async def get_entry_price(self, inst: str, side: str) -> float:
        async with self._lock:
            return self.entry_prices.get((inst, side), 0.0)

    async def get_order_status(self, ord_id: str) -> Optional[str]:
        async with self._lock:
            return self.orders.get(ord_id)

    async def get_mark_price(self, inst: str) -> float:
        return self._last_mark_px
    
    async def get_last_price(self, inst: str) -> float:
            """
            Возвращает последний полученный trade-прайс
            """
            try:
                return self._last_mark_px
            except KeyError:
                raise RuntimeError(f"No trade data for {inst!r} yet")
            

    # Вызвать из твоего ws-reader'а, когда пришёл апдейт по ордерам/исполнениям
    async def on_ws_message(self, raw: dict[str, Any]) -> None:
        ev = self._normalize_algo_fill(raw)
        if not ev:
            logger.debug("[WS] _normalize_algo_fill → None (не релевантно)")
            return
        logger.debug(
        "[WS] NORM ev: "
        f"algoClOrdId={ev.algoClOrdId} clOrdId={ev.clOrdId} ordId={ev.ordId} "
        f"side={ev.side} posSide={ev.posSide} fillPx={ev.fillPx} fillSz={ev.fillSz} "
        f"ordType={ev.ordType} is_oco={ev.is_oco}"
            )   
        if not getattr(self, "_algo_listeners", None):
            logger.warning("[WS] Нет зарегистрированных algo_listeners")
            return

        logger.debug(f"[WS] Отправляю в {len(self._algo_listeners)} listeners")
        # Рассылаем всем подписчикам; поддержим sync и async коллбеки
        for cb in list(self._algo_listeners):
            try:
                logger.debug(f"[WS] → listener {getattr(cb, '__name__', cb)}")
                res = cb(ev)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)  # не блокируем поток чтения WS
            except Exception:
                # Логируй и продолжай остальных слушателей
                logger.error("[WS] Error in algo-listener", extra={"mode": "WS"}, exc_info=e)
                pass

    def _normalize_algo_fill(self, raw: dict[str, Any]) -> AlgoFillEvent | None:
        """
        Приводим сырое сообщение к единой модели.
        Здесь нужно сопоставить поля конкретной биржи (OKX/Bybit/...).
        Ниже — пример маппинга с дефолтами.
        """
        # Часто полезно работать с одним словарём-источником:
        d = raw.get("data", raw)  # если биржа кладёт полезную нагрузку в "data"
        try:
            algo_cl_id = str(d.get("algoClOrdId") or d.get("algoClientOid") or "") or None
            cl_id      = str(d.get("clOrdId") or d.get("clientOid") or "") or None
            ord_id     = str(d.get("ordId") or d.get("orderId") or "") or None

            side       = str(d.get("side") or "").upper()          # BUY/SELL
            pos_side   = (str(d.get("posSide") or "").lower() or None)  # long/short/None

            # Размеры
            fill_px    = float(d.get("fillPx") or d.get("execPx") or 0)
            fill_sz    = float(d.get("fillSz") or d.get("execSz") or d.get("lastFillQty") or 0)
            acc_fill   = d.get("accFillSz") or d.get("cumExecQty")
            acc_fill   = float(acc_fill) if acc_fill is not None else None

            ord_type   = (str(d.get("ordType") or d.get("orderType") or "").lower() or None)

            # Признак OCO: лучше вычислять в мониторинге один раз
            is_oco     = (
                (ord_type == "oco") or
                bool(d.get("tpTriggerPx")) or  # для ряда бирж наличие tp/sl-полей указывает на oco-связку
                bool(d.get("slTriggerPx")) or
                str(d.get("algoType") or "").lower() == "oco"
            )

            ts        = int(d.get("ts") or d.get("timestamp") or 0) or None

            # Отсечём нерелевантные события — нам нужны именно исполнения TP/SL/ордеров
            if fill_px <= 0 or (fill_sz <= 0 and (acc_fill or 0) <= 0):
                return None

            return AlgoFillEvent(
                algoClOrdId=algo_cl_id,
                clOrdId=cl_id,
                ordId=ord_id,
                side=side,
                posSide=pos_side,
                fillPx=fill_px,
                fillSz=fill_sz,
                accFillSz=acc_fill,
                ordType=ord_type,
                is_oco=is_oco,
                ts=ts,
            )
        except Exception:
            return None