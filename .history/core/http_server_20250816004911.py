# core/http_server.py
import asyncio, logging
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from core.logger import logger, formatter, ContextFilter
from core.task_tracker import poll_tracked_tasks

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
    # лог из логгера "bot"________________    
    #logger = logging.getLogger("bot")

    handler.setFormatter(formatter) 
    handler.addFilter(ContextFilter())  # Заполняет недостающие поля  
    logger = logging.getLogger()  #  root или "uvicorn" или "bot"
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.addHandler(handler)
        uv_logger.propagate = False
    

    try:
        yield
    finally:
        logger.removeHandler(handler)        
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).removeHandler(handler)

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="core/templates")

class _WSLogHandler(logging.Handler):
    def __init__(self, broadcaster: LogBroadcaster):
        super().__init__()
        self.broadcaster = broadcaster
    def emit(self, record):
        msg = self.format(record)
        asyncio.create_task(self.broadcaster.push(msg))

#app = FastAPI()


@app.websocket("/ws")
async def ws_log(ws: WebSocket):
    await broadcaster.connect(ws)

    # При подключении отправляем полный массив задач
    await ws.send_text(json.dumps({
        "type": "tasks",
        "data": poll_tracked_tasks()
    }, ensure_ascii=False))

    try:
        while True:
            try:
                _ = await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                pass
            # новый опрос перед каждой отправкой
            await ws.send_text(json.dumps({
                "type": "tasks",
                "data": poll_tracked_tasks()
            }, ensure_ascii=False))
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