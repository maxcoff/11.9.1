# core/http_server.py
import asyncio, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from core.logger import logger, formatter

# WebSocket-менеджер
class LogBroadcaster:
    def __init__(self):
        self.active: set[WebSocket] = set()

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

broadcaster = LogBroadcaster()



@asynccontextmanager
async def lifespan(app: FastAPI):
    handler = _WSLogHandler(broadcaster)
    handler.setLevel(logging.DEBUG)    
    #  лог uvicorn_________________
    #uvicorn_logger = logging.getLogger("uvicorn")            
    # лог root__________________    
    #root_logger = logging.getLogger()
    #root_logger.addHandler(handler)
    #root_logger.setLevel(logging.DEBUG)
    # лог из логгера "bot"________________    
    
    handler.setFormatter(formatter)    
    
    logger = logging.getLogger()
    #logger = logging.getLogger("bot")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        #root_logger.removeHandler(handler)
        #uvicorn_logger.removeHandler(handler)

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="core/templates")

class _WSLogHandler(logging.Handler):
    def __init__(self, broadcaster: LogBroadcaster):
        super().__init__()
        self.broadcaster = broadcaster
    def emit(self, record):
        msg = self.format(record)
        asyncio.create_task(self.broadcaster.push(msg))




@app.websocket("/ws")
async def ws_log(ws: WebSocket):
    await broadcaster.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        broadcaster.disconnect(ws)

@app.get("/", response_class=HTMLResponse)
async def index():
    return templates.TemplateResponse("index.html", {"request": {}})

# public-функция для запуска сервера
async def run_server(host="192.168.100.254", port=8000):
    import uvicorn
    config = uvicorn.Config("core.http_server:app", host=host, port=port, reload=True)
    server = uvicorn.Server(config)
    await server.serve()