# http_server.py
# FastAPI + WebSocket панель управления:
# - /                — простая веб-страница с UI
# - /api/status      — статус оркестратора
# - /api/start       — старт оркестратора (фоновая задача)
# - /api/stop        — остановка оркестратора (если есть .stop(), иначе cancel)
# - /api/restart     — рестарт
# - /api/command     — произвольная команда в оркестратор
# - /ws/logs         — WebSocket поток логов
#
# Авторизация: токен через заголовок X-Admin-Token (HTTP) и ?token=... (WS)
# Токен берётся из переменной окружения ADMIN_TOKEN (по умолчанию: "change-me")

import os
import asyncio
import logging
import inspect
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
from core.logger            import logger
# ---------- Глобальное состояние ----------

class _State:
    orchestrator: Any = None
    orch_task: Optional[asyncio.Task] = None
    logger: logging.Logger = logging.getLogger("bot")
    admin_token: str = os.environ.get("ADMIN_TOKEN", "change-me")
    startup_ts: datetime = datetime.now(timezone.utc)
    loop: Optional[asyncio.AbstractEventLoop] = None
    broadcaster_task: Optional[asyncio.Task] = None
    log_handler: Optional[logging.Handler] = None
    http_server_task: Optional[asyncio.Task] = None
state = _State()

# ---------- Broadcaster логов ----------

class LogBroadcaster:
    def __init__(self, max_buffer: int = 1000):
        self._subs: set[WebSocket] = set()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._buffer: list[str] = []
        self._max_buffer = max_buffer

    async def run(self):
        while not self._stop.is_set():
            msg = await self._queue.get()
            # Буфер для "tail"
            self._buffer.append(msg)
            if len(self._buffer) > self._max_buffer:
                self._buffer = self._buffer[-self._max_buffer:]
            # Отправка всем подписчикам
            dead = []
            for ws in list(self._subs):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                await self.unregister(ws)

    def enqueue(self, msg: str):
        # Безопасно из любого потока
        if state.loop is not None:
            state.loop.call_soon_threadsafe(self._queue.put_nowait, msg)

    async def register(self, ws: WebSocket):
        self._subs.add(ws)

    async def unregister(self, ws: WebSocket):
        if ws in self._subs:
            self._subs.remove(ws)
            with contextlib.suppress(Exception):
                await ws.close()

    def get_tail(self, n: int = 200) -> list[str]:
        return self._buffer[-n:]

import contextlib
broadcaster = LogBroadcaster()

class WebLogHandler(logging.Handler):
    def __init__(self, formatter: Optional[logging.Formatter] = None):
        super().__init__()
        if formatter:
            self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        broadcaster.enqueue(msg)

# ---------- Безопасность ----------

def _require_token(token: Optional[str]):
    if not token or token != state.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

async def require_http_token(request: Request):
    #token = request.headers.get("X-Admin-Token")
    #_require_token(token)
    return

# ---------- Модели ----------

class CommandIn(BaseModel):
    command: str
    payload: Optional[Dict[str, Any]] = None

class StatusOut(BaseModel):
    running: bool
    uptime_seconds: float
    details: Dict[str, Any] = {}

# ---------- Утилиты вызова оркестратора ----------

async def maybe_call(obj: Any, name: str, *args, **kwargs):
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result

def is_running() -> bool:
    # 1) Явный флаг/метод, если есть
    for attr in ("is_running", "running"):
        val = getattr(state.orchestrator, attr, None)
        if isinstance(val, bool):
            return val
        if callable(val):
            try:
                r = val()
                if isinstance(r, bool):
                    return r
            except Exception:
                pass
    # 2) По задаче
    return bool(state.orch_task and not state.orch_task.done())

def build_status() -> StatusOut:
    orch = state.orchestrator
    logger.info(f"build_status: orch_id={id(orch)} http_state_id={id(state)}")
    details = {"__probe__": "alive"}
    #details = {}
    if orch is not None:
        get_st = getattr(orch, "get_status", None)
        if callable(get_st):
            try:
                val = get_st()  # если у тебя async def get_status, тут нужно await (и тогда build_status станет async)
                if isinstance(val, dict):
                    details = val
            except Exception:
                state.logger.exception("get_status() failed")

    running = bool(details.get("running")) or bool(state.orch_task and not state.orch_task.done())
    uptime = (datetime.now(timezone.utc) - state.startup_ts).total_seconds()
    return StatusOut(running=running, uptime_seconds=uptime, details=details)


# ---------- Приложение ----------

app = FastAPI(title="Bot Control Panel", version="1.0")

@app.on_event("startup")
async def on_startup():
    state.loop = asyncio.get_running_loop()
    # Подключаем лог-хендлер для трансляции в WS
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%d-%m %H:%M:%S")
    handler = WebLogHandler(formatter=fmt)
    handler.setLevel(logging.INFO)
    state.log_handler = handler
    state.logger.addHandler(handler)

    # Стартуем фонового broadcaster
    state.broadcaster_task = asyncio.create_task(broadcaster.run())    
    state.logger.info(
        "HTTP started. Orchestrator attached: %s, has get_status: %s",
        type(state.orchestrator).__name__ if state.orchestrator else None,
        hasattr(state.orchestrator, "get_status"),
    )

@app.on_event("shutdown")
async def on_shutdown():
    # Отключаем лог-хендлер
    if state.log_handler:
        with contextlib.suppress(Exception):
            state.logger.removeHandler(state.log_handler)
    # Останавливаем broadcaster
    if state.broadcaster_task:
        with contextlib.suppress(Exception):
            state.broadcaster_task.cancel()
            await state.broadcaster_task

# ---------- HTML UI ----------

INDEX_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <title>Status & Logs</title>
  <style>
    body { font-family: sans-serif; margin: 20px; background: #f4f4f4; }
    h1 { margin-top: 0; }
    pre { background: #222; color: #0f0; padding: 10px; max-height: 300px; overflow-y: auto; }
    #details { background: #fff; padding: 10px; white-space: pre-wrap; }
  </style>
</head>
<body>
  <h1>Status</h1>
  <div>
    <b>Uptime:</b> <span id="uptime">—</span>
  </div>
  <div>
    <b>Details:</b>
    <div id="details"></div>
  </div>

  <h2>Logs</h2>
  <pre id="logs"></pre>

  <script>
    async function refreshStatus() {
      try {
        const res = await fetch('/api/status');
        const st = await res.json();
        document.getElementById('uptime').textContent = st.uptime || '—';
        document.getElementById('details').textContent = JSON.stringify(st.details, null, 2);
      } catch (err) {
        console.error('Status error:', err);
      }
    }

    function connectLogs() {
      const ws = new WebSocket(`ws://${location.host}/api/logs`);
      const logsEl = document.getElementById('logs');

      ws.onmessage = e => {
        logsEl.textContent += e.data + '\n';
        logsEl.scrollTop = logsEl.scrollHeight;
      };

      ws.onclose = () => {
        setTimeout(connectLogs, 2000); // пробуем переподключиться
      };
    }

    refreshStatus();
    setInterval(refreshStatus, 1000);
    connectLogs();
  </script>
</body>
</html>

"""

# ---------- Роуты ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)

@app.get("/api/status", response_model=StatusOut, dependencies=[Depends(require_http_token)])
async def api_status():
    return build_status()

@app.post("/api/start", dependencies=[Depends(require_http_token)])
async def api_start():
    if is_running():
        return JSONResponse({"status": "already-running"})
    # run() как фоновая задача
    state.orch_task = asyncio.create_task(maybe_call(state.orchestrator, "run"))
    return JSONResponse({"status": "started"})

@app.post("/api/stop", dependencies=[Depends(require_http_token)])
async def api_stop():
    #if not is_running():
    #    return JSONResponse({"status": "already-stopped"})
    # Если у оркестратора есть корректный stop(), используем его
    res = await maybe_call(state.orchestrator, "_shutdown")
    # Если stop() нет — отменяем задачу
    if state.orch_task and not state.orch_task.done():
        state.orch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.orch_task
    return JSONResponse({"status": "stopped", "result": res})

@app.post("/api/restart", dependencies=[Depends(require_http_token)])
async def api_restart():
    await api_stop()
    await asyncio.sleep(0.1)
    await api_start()
    return JSONResponse({"status": "restarted"})

@app.post("/api/command", dependencies=[Depends(require_http_token)])
async def api_command(cmd: CommandIn):
    # Ищем метод: handle_command / command / dispatch
    for name in ("handle_command", "command", "dispatch"):
        if hasattr(state.orchestrator, name):
            res = await maybe_call(state.orchestrator, name, cmd.command, cmd.payload)
            return JSONResponse({"ok": True, "result": res})
    raise HTTPException(status_code=400, detail="No command handler in orchestrator")

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    # Токен через query
    token = ws.query_params.get("token")
    try:
        _require_token(token)
    except HTTPException:
        await ws.close(code=1008)
        return
    await ws.accept()
    await broadcaster.register(ws)

    # Отправим хвост последних логов
    for line in broadcaster.get_tail(200):
        with contextlib.suppress(Exception):
            await ws.send_text(line)

    try:
        # Держим соединение (при желании можем принимать pings/keepalive)
        while True:
            # Клиент может слать пинги; мы их просто читаем
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.unregister(ws)

# ---------- API для встраивания в основной процесс ----------

def attach(orchestrator: Any, logger: Optional[logging.Logger] = None, admin_token: Optional[str] = None):
    """
    Присоединить объекты в общее состояние (для одного процесса).
    """
    state.orchestrator = orchestrator
    if logger:
        state.logger = logger
    if admin_token:
        state.admin_token = admin_token

async def start_http_server(orchestrator: Any, logger: Optional[logging.Logger] = None,
                            host: str = "127.0.0.1", port: int = 8000, admin_token: Optional[str] = None):
    """
    Программиремый запуск uvicorn в фоновом таске того же event loop.
    """
    attach(orchestrator, logger, admin_token)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)
    # serve() — асинхронный метод; запускаем как фон
    #return asyncio.create_task(server.serve())

    task = asyncio.create_task(server.serve())
    state.http_server_task = task
    return task

# Позволяет запускать как отдельный процесс: uvicorn http_server:app --reload
# Перед этим нужно, чтобы attach() был вызван или чтобы оркестратор создавался здесь,
# но в большинстве случаев вы будете стартовать сервер из основного приложения.
