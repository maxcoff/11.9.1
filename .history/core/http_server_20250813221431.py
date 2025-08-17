import asyncio
from fastapi import FastAPI, WebSocket, Depends
from fastapi.responses import HTMLResponse
from core.logger import logger

app = FastAPI()

# Простейший HTML (для старта)
@app.get("/")
async def root():
    return HTMLResponse("<h1>Bot control panel</h1>")

@app.post("/start")
async def start_bot():
    asyncio.create_task(orchestrator.run())
    return {"status": "started"}

@app.post("/stop")
async def stop_bot():
    await orchestrator.stop()
    return {"status": "stopped"}

@app.websocket("/logs")
async def stream_logs(ws: WebSocket):
    await ws.accept()
    q = asyncio.Queue()

    class WSHandler:
        def emit(self, msg):
            asyncio.create_task(q.put(msg))

    log_handler = WSHandler()
    logger.addHandler(log_handler)

    try:
        while True:
            msg = await q.get()
            await ws.send_text(msg)
    except Exception:
        pass
