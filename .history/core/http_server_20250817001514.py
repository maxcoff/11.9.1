# core/http_server.py
import asyncio, logging
import json
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from core.logger import logger, formatter, ContextFilter
from core.task_manager import TaskManager
from core.ws_events import broadcaster
from main import task_manager




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
    def __init__(self, broadcaster):
        super().__init__()
        self.broadcaster = broadcaster
    def emit(self, record):
        msg = self.format(record)
        asyncio.create_task(self.broadcaster.push(msg))

#app = FastAPI()


@app.websocket("/ws")
async def ws_log(ws: WebSocket):
    await broadcaster.connect(ws)

    async def send_tasks():
        snapshot = task_manager.list()
        print(f"[{datetime.now().isoformat()}] Отправляем {len(snapshot)} задач:")
        for entry in snapshot:
            print(f"  {entry['name']}: {entry['state']}")

    await ws.send_text(json.dumps({"type": "tasks", "data": task_manager.list()}, ensure_ascii=False))
    
    print("task_manager id:", id(task_manager))
    await send_tasks()

    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=10)
            except asyncio.TimeoutError:
                pass
            await ws.send_text(json.dumps({"type": "tasks", "data": task_manager.list()}, ensure_ascii=False))            
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




