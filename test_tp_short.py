import asyncio
import aiohttp
import hmac
import hashlib
import base64
import json
from datetime import datetime, timezone

import config
import sys
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REST_URL = "https://www.okx.com"
ALGO_PATH = "/api/v5/trade/order-algo"


def timestamp():
    dt = datetime.now(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sign(ts, method, path, body=""):
    msg = ts + method + path + body
    mac = hmac.new(config.get("API_SECRET").encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


async def place_conditional_tp():
    async with aiohttp.ClientSession() as session:
        inst_id   = config.get("INSTRUMENT", "DOGE-USDT-SWAP")
        td_mode   = config.get("TD_MODE", "cross")
        tp_px     = config.get("TP_PRICE", "0.16250")   # 🎯 Цель
        qty       = config.get("TP_SIZE", "0.05")       # ⚖️ Закрываемая позиция

        ts  = timestamp()
        body = {
            "instId":        inst_id,
            "tdMode":        td_mode,
            "side":          "buy",           # ← для закрытия short
            "posSide":       "short",
            "ordType":       "conditional",
            "algoOrdType":   "conditional",
            "tpTriggerPx":   tp_px,
            "tpOrdPx":       "-1",             # ← market
            "triggerPxType": "last",
            "sz":            qty,
            "reduceOnly":    True
        }

        raw = json.dumps(body)
        sgn = sign(ts, "POST", ALGO_PATH, raw)
        headers = {
            "OK-ACCESS-KEY":        config.get("API_KEY"),
            "OK-ACCESS-SIGN":       sgn,
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": config.get("PASSPHRASE"),
            "Content-Type":         "application/json"
        }
        if config.get("USE_DEMO", "false").lower() == "true":
            headers["x-simulated-trading"] = "1"

        async with session.post(REST_URL + ALGO_PATH, headers=headers, json=body) as r:
            resp = await r.json()
            print("TP Response:", resp)
            return resp


if __name__ == "__main__":
    asyncio.run(place_conditional_tp())
