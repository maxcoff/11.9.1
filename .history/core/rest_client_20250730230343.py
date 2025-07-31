# rest_client.py

import aiohttp
from aiohttp import ClientTimeout, ClientResponseError
import asyncio
import time
import hmac
import hashlib
import base64
import json
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from core.config import get
from core.logger import logger


class RestClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        session: aiohttp.ClientSession,
        base_url: str,
        use_demo: bool,
        logger=logger
    ):
        self.api_key     = api_key
        self.api_secret  = api_secret
        self.passphrase  = passphrase
        self.session     = session
        self.base_url    = base_url.rstrip("/")
        self.use_demo    = use_demo
        self.logger      = logger

        self.time_offset = 0  # текущее смещение в мс

        self._sync_interval    = int(get("TIME_SYNC_INTERVAL")      or "10")
        self._offset_threshold = int(get("TIME_OFFSET_THRESHOLD_MS") or "2000")

        # запускаем фоновую синхронизацию времени
        loop = asyncio.get_running_loop()
        self._sync_task = loop.create_task(self._time_sync_loop())

    async def sync_rest_time(self) -> int:
        """
        GET /api/v5/public/time
        возвращает server_ts в миллисекундах
        """
        path    = "/api/v5/public/time"
        url     = f"{self.base_url}{path}"
        timeout = ClientTimeout(total=10)
        async with self.session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            data      = await resp.json()
            server_ts = int(data["data"][0]["ts"])
            print (f"Server time: {server_ts} ms")
            return server_ts

    async def _time_sync_loop(self):
        """
        Фон: каждые self._sync_interval сек дергаем серверное время,
        считаем новый offset и обновляем, если разница > threshold.
        """
        while True:
            try:
                server_ts   = await self.sync_rest_time()
                local_ts    = int(time.time() * 1000)
                new_offset  = server_ts - local_ts

                if abs(new_offset - self.time_offset) > self._offset_threshold:
                    old = self.time_offset
                    self.time_offset = new_offset
                    self.logger.info(
                        f"Time offset updated {old} → {new_offset} ms",
                        extra={"mode": "REST"}
                    )
            except Exception as e:
                self.logger.warning(
                    f"Time sync failed: {e}",
                    extra={"mode": "REST", "errorCode": "-"}
                )
            await asyncio.sleep(self._sync_interval)

    def _iso_ts(self) -> str:
        """
        Возвращает ISO-8601 UTC millisecond timestamp,
        скорректированный на self.time_offset.
        """
        dt = datetime.now(timezone.utc) + timedelta(milliseconds=self.time_offset)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _sign(self, ts: str, method: str, path: str, body_str: str = "") -> str:
        """
        Формируем OKX API v5 подпись.
        """
        msg = ts + method.upper() + path + body_str
        h = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(h.digest()).decode()

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Основной метод обращения к OKX API v5.
        Использует self._iso_ts() (с учетом offset) для OK-ACCESS-TIMESTAMP.
        """
        url = self.base_url + path
        body_str = json.dumps(data, separators=(",", ":")) if data else ""

        # собираем query-string для подписи
        if params:
            qs = urllib.parse.urlencode(params, doseq=True)
            request_path = f"{path}?{qs}"
        else:
            request_path = path

        for attempt in range(2):
            ts        = self._iso_ts()
            sign      = self._sign(ts, method, request_path, body_str)
            headers   = {
                "OK-ACCESS-KEY":        self.api_key,
                "OK-ACCESS-SIGN":       sign,
                "OK-ACCESS-TIMESTAMP":  ts,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type":         "application/json"
            }
            if self.use_demo:
                headers["x-simulated-trading"] = "1"

            timeout = ClientTimeout(total=10)
            try:
                async with self.session.request(
                    method,
                    url,
                    params=params,
                    data=body_str.encode() if body_str else None,
                    headers=headers,
                    timeout=timeout
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 500:
                        self.logger.error(
                            f"HTTP {resp.status} error: {text}",
                            extra={"mode": "REST", "errorCode": str(resp.status)}
                        )
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.json()
            except ClientResponseError as e:
                self.logger.error(
                    f"HTTP error {e.status}: {e.message}",
                    extra={"mode": "REST", "errorCode": str(e.status)},
                    exc_info=e
                )
                raise
            except asyncio.TimeoutError as e:
                self.logger.error(
                    f"Timeout {method} {url}",
                    extra={"mode": "REST", "errorCode": "-"},
                    exc_info=e
                )
                raise
            except json.JSONDecodeError as e:
                self.logger.error(
                    f"Invalid JSON on {method} {url}: {e}",
                    extra={"mode": "REST"},
                    exc_info=e
                )
                raise

            if data is None:
                self.logger.error(
                    f"Response data is None on {method} {url}",
                    extra={"mode": "REST"}
                )
                raise RuntimeError("Response data is None")
            code = str(data.get("code", "0"))
            nested = [
                str(d.get("sCode"))
                for d in data.get("data", [])
                if d.get("sCode") is not None
            ]

            # swallow benign errors
            if (
                code == "51000"
                or any(c in ("51000", "51088", "51278", "51280") for c in nested)
            ):
                self.logger.warning(
                    f"[REST] swallow code={code}, sCode={nested} on {method} {path}",
                    extra={"mode": "REST", "errorCode": code}
                )
                return {"data": []}

            # retry timestamp errors
            if code in ("40001", "50102") and attempt == 0:
                self.logger.info(
                    f"Timestamp expired ({code}), retrying once",
                    extra={"mode": "REST", "errorCode": code}
                )
                continue

            if code != "0":
                self.logger.error(
                    f"API error code={code} response={data}",
                    extra={"mode": "REST", "errorCode": code}
                )
                raise RuntimeError(f"API error {code}")

            return data

        raise RuntimeError(f"REST request failed after retries on {method} {path}")

    async def fetch_snapshots(self, instrument: str, sz: int = 5) -> Dict[str, Any]:
        """
        Удобный wrapper для GET /api/v5/market/books.
        """
        return await self.request("GET", "/api/v5/market/books", params={"instId": instrument, "sz": sz})

    async def close(self):
        """
        Останавливаем тайм-синк таск и закрываем HTTP сессию.
        """
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        await self.session.close()
