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

    def __init__(self, rest, ws_monitor, order_client, tpsl_monitor, task_manager):
        self.task_manager = task_manager
        self.rest   = rest
        self.ws     = ws_monitor
        self.orders = order_client
        self.tpsl   = tpsl_monitor

        # чтобы не запускать несколько ре-инвестов одновременно
        self._lock = asyncio.Lock()

    
    async def handle_tp_fill(self, qty: float, hedge_side: str) -> None:
        """
        qty — объём, закрытый TP-OCO по исходной ноге.
        hedge_side — 'long' или 'short' для оставшейся хедж-ноги.
        Делаем зеркальный маркет на ту же величину.
        """
        async with self._lock:
            side_ord = "sell" if hedge_side == "long" else "buy"
            resp = await self.orders.place_order(
                side=side_ord,
                order_type="market",
                qty=qty,
                pos_side=hedge_side,
            )

        print (resp)    
            # Логируем «сырой» ответ для отладки
        import json
        logger.debug(f"[REINVEST-RAW] {json.dumps(resp, ensure_ascii=False)}")

        # Проверяем код ответа
        code = resp.get("code")
        if code != "0":
            logger.error(f"[REINVEST-ERR] code={code} msg={resp.get('msg')}")
        else:
            logger.info(f"[REINVEST-OK] market {side_ord} qty={qty} pos_side={hedge_side}")


    