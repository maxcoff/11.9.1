import asyncio
import json
import hmac
import base64
import time
import websockets
from core.config       import get, get_bool



API_KEY    = get("API_KEY_DEMO")     or get("API_KEY")
API_SECRET = get("API_SECRET_DEMO")  or get("API_SECRET")
API_PASS = get("PASSPHRASE_DEMO")  or get("PASSPHRASE")

WS_URL = "wss://ws.okx.com:8443/ws/v5/private"

def get_signature(timestamp, method, request_path, body, secret_key):
    message = f"{timestamp}{method}{request_path}{body}"
    mac = hmac.new(secret_key.encode(), message.encode(), digestmod="sha256")
    return base64.b64encode(mac.digest()).decode()

async def connect():
    async with websockets.connect(WS_URL) as ws:
        # аутентификация
        ts = str(time.time())
        sign = get_signature(ts, 'GET', '/users/self/verify', '', API_SECRET)
        login_args = {
            "op": "login",
            "args": [{
                "apiKey": API_KEY,
                "passphrase": API_PASS,
                "timestamp": ts,
                "sign": sign
            }]
        }
        await ws.send(json.dumps(login_args))
        print("-> login sent")

        # читаем ответ на логин
        print("<-", await ws.recv())

        # подписка на приватный канал positions
        sub_args = {
            "op": "subscribe",
            "args": [{"channel": "positions"}]
        }
        await ws.send(json.dumps(sub_args))
        print("-> subscribed to positions")

        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            print("<-", json.dumps(data, ensure_ascii=False, indent=2))

asyncio.run(connect())
