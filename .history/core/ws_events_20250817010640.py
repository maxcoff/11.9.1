# core/ws_events.py
from typing import Set
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
import asyncio

class LogBroadcaster:
    def __init__(self):
        self.active: Set[WebSocket] = set()
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def push(self, msg: str):
        for ws in list(self.active):
            try:
                await ws.send_text(msg)
            except WebSocketDisconnect:
                self.active.remove(ws)
    
    async def broadcast(self, message: str) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_text(message)
            except Exception:
                # обработка ошибок
                pass

# Один общий экземпляр для всего проекта
broadcaster = LogBroadcaster()
