# core/ws_events.py
from typing import Set
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
import asyncio

class LogBroadcaster:
    def __init__(self):
        self.active: Set[WebSocket] = set()

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

# Один общий экземпляр для всего проекта
broadcaster = LogBroadcaster()
