# okx_trades_probe.py
import asyncio
import json
import sys
from datetime import datetime, timezone

try:
    import websockets  # pip install websockets
except ImportError:
    print("pip install websockets", file=sys.stderr)
    raise

LIVE_PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"
DEMO_PUBLIC_WS = "wss://wspap.okx.com:8443/ws/v5/public"

# ← укажи свой инструмент. Для деривативов это обычно ***-SWAP, напр. "BTC-USDT-SWAP"
#INST_ID = "DOGE-USDT"        # спот
INST_ID = "DOGE-USDT-SWAP" # перп

USE_DEMO = True            # True — демо, False — боевой public

SUB_ACK_TIMEOUT = 5.0       # сек: ждём подтверждение подписки
RUN_TIME = 30.0             # сколько секунд слушать поток сделок


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


async def probe_trades(inst_id: str, demo: bool = False):
    uri = DEMO_PUBLIC_WS if demo else LIVE_PUBLIC_WS
    print(f"{ts()} CONNECT → {uri} | instId={inst_id} | channel=trades")

    async with websockets.connect(uri, max_size=2**20) as ws:
        # отправляем подписку
        sub_msg = {
            "op": "subscribe",
            "args": [{"channel": "trades", "instId": inst_id}],
        }
        await ws.send(json.dumps(sub_msg))
        print(f"{ts()} SUBSCRIBE → {sub_msg}")

        # ждём ACK/ERROR ограниченное время
        ack_ok = False
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=SUB_ACK_TIMEOUT)
                try:
                    obj = json.loads(msg)
                except json.JSONDecodeError:
                    print(f"{ts()} NON-JSON ← {msg!r}")
                    continue

                # Ошибка подписки
                if isinstance(obj, dict) and obj.get("event") == "error":
                    print(f"{ts()} ERROR_EVT ← {obj}")
                    break

                # Подтверждение подписки
                if isinstance(obj, dict) and obj.get("event") == "subscribe":
                    arg = obj.get("arg", {})
                    if arg.get("channel") == "trades" and arg.get("instId") == inst_id:
                        ack_ok = True
                        print(f"{ts()} SUBSCRIBED ✓ → {arg}")
                        break

                # Некоторые сервера шлют сначала данные, потом ACK — на всякий случай
                if isinstance(obj, dict) and obj.get("arg", {}).get("channel") == "trades":
                    ack_ok = True
                    print(f"{ts()} SUBSCRIBED? (data before ack) → {obj.get('arg')}")
                    # не выходим: продолжим слушать данные
                    break
        except asyncio.TimeoutError:
            print(f"{ts()} ACK TIMEOUT ✗ — подтверждение не пришло за {SUB_ACK_TIMEOUT}s")

        if not ack_ok:
            print(f"{ts()} STOP — trades не подтверждён. Проверь instId/endpoint.")
            return

        # слушаем поток сделок фиксированное время
        print(f"{ts()} LISTEN → {RUN_TIME}s (channel=trades, instId={inst_id})")
        end = asyncio.get_event_loop().time() + RUN_TIME
        while asyncio.get_event_loop().time() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=RUN_TIME)
            except asyncio.TimeoutError:
                print(f"{ts()} TIMEOUT без сообщений")
                break

            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                continue

            # OKX формат: { arg: {...}, data: [ { px, sz, side, ts, ... }, ... ] }
            if not isinstance(obj, dict):
                continue

            arg = obj.get("arg", {})
            if arg.get("channel") != "trades" or arg.get("instId") != inst_id:
                # игнорируем другие каналы, если вдруг придут
                continue

            for rec in obj.get("data", []):
                px = rec.get("px")
                sz = rec.get("sz")
                side = rec.get("side")
                tsm = rec.get("ts")
                try:
                    # сделаем вывод дружелюбным
                    px_f = float(px) if px is not None else None
                    sz_f = float(sz) if sz is not None else None
                    ts_h = datetime.fromtimestamp(int(tsm)/1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3] if tsm else "—"
                except Exception:
                    px_f, sz_f, ts_h = px, sz, tsm
                print(f"{ts()} TRADE ← px={px_f} sz={sz_f} side={side} trade_ts={ts_h}")

        print(f"{ts()} DONE")


if __name__ == "__main__":
    # Позволим быстро переключить режимы из командной строки:
    #   python okx_trades_probe.py BTC-USDT False
    #   python okx_trades_probe.py BTC-USDT-SWAP True
    if len(sys.argv) >= 2:
        INST_ID = sys.argv[1]
    if len(sys.argv) >= 3:
        USE_DEMO = sys.argv[2].lower() in ("1", "true", "yes", "y")

    asyncio.run(probe_trades(INST_ID, demo=USE_DEMO))
