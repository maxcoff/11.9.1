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
    token = request.headers.get("X-Admin-Token")
    _require_token(token)

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
    details: Dict[str, Any] = {}
    # Попробуем вытащить детальный статус, если есть
    if orch is not None:
        # 1) Пытаемся взять срез из оркестратора
        get_st = getattr(orch, "get_status", None)
        if callable(get_st):
            try:
                val = get_st()
                if isinstance(val, dict):
                    details = val
            except Exception as e:
                state.logger.warning("get_status() failed: %s", e)

    # 2) Бэкап: если оркестратор не дал 'running', берём из задачи
    running = bool(details.get("running", False))
    if not running:
        running = bool(state.orch_task and not state.orch_task.done())

    # 3) Аптайм оставим как есть (или от времени присоединения)
    uptime = (datetime.now(timezone.utc) - state.startup_ts).total_seconds()

    state.logger.info("DEBUG details: %r", details)

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
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Bot Control Panel</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, Arial, sans-serif; margin: 16px; }
    .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    button { padding: 8px 12px; }
    #logs { background: #0b0f14; color: #cfe3ff; padding: 12px; height: 50vh; overflow: auto; white-space: pre-wrap; border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .tag { display:inline-block; padding:2px 6px; border-radius:4px; background:#eee; margin-right:6px; font-size:12px;}
    input[type=text] { padding:6px; width: 280px; }
    textarea { width: 280px; height: 80px; }
    .muted { color: #666; }
  </style>
</head>
<body>
  <h1>Bot Control Panel</h1>
  <div class="row">
    <div>Status: <span id="status" class="tag">—</span></div>
    <div>Uptime: <span id="uptime" class="muted">0s</span></div>
    <button id="btnStart">Start</button>
    <button id="btnStop">Stop</button>
    <button id="btnRestart">Restart</button>
  </div>

  <h3>Logs</h3>
  <div id="logs"></div>

  <h3>Details</h3>
    <pre id="details"></pre>

    <script>
    async function refreshStatus() {
        try {
        const st = await api("/api/status");
        document.getElementById("status").textContent = st.running ? "RUNNING" : "STOPPED";
        document.getElementById("status").style.background = st.running ? "#d1fae5" : "#fde2e2";
        document.getElementById("uptime").textContent = formatSeconds(st.uptime_seconds);

        // Новое: показываем details как JSON
        document.getElementById("details").textContent = JSON.stringify(st.details, null, 2);
        } catch (e) {
        console.error(e);
        }
    }
    </script>

  <h3>Command</h3>
  <div class="row">
    <input id="cmd" type="text" placeholder="command name">
    <textarea id="payload" placeholder='{"key":"value"}'></textarea>
    <button id="btnSend">Send</button>
  </div>

  <script>
    const TOKEN = localStorage.getItem("ADMIN_TOKEN") || prompt("Admin token:") || "";
    localStorage.setItem("ADMIN_TOKEN", TOKEN);

    async function api(path, options={}) {
      options.headers = Object.assign({
        "X-Admin-Token": TOKEN,
        "Content-Type": "application/json"
      }, options.headers || {});
      const res = await fetch(path, options);
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(txt || res.statusText);
      }
      return res.json();
    }

    function formatSeconds(s) {
      s = Math.floor(s);
      const h = Math.floor(s/3600);
      const m = Math.floor((s%3600)/60);
      const sec = s % 60;
      return (h>0? h+"h ":"") + (m>0? m+"m ":"") + sec + "s";
    }

    async function refreshStatus() {
      try {
        const st = await api("/api/status");
        document.getElementById("status").textContent = st.running ? "RUNNING" : "STOPPED";
        document.getElementById("status").style.background = st.running ? "#d1fae5" : "#fde2e2";
        document.getElementById("uptime").textContent = formatSeconds(st.uptime_seconds);
      } catch (e) {
        console.error(e);
      }
    }

    async function start() { await api("/api/start", {method: "POST"}); refreshStatus(); }
    async function stop()  { await api("/api/stop", {method: "POST"});  refreshStatus(); }
    async function restart(){ await api("/api/restart", {method: "POST"}); refreshStatus(); }

    document.getElementById("btnStart").onclick = start;
    document.getElementById("btnStop").onclick = stop;
    document.getElementById("btnRestart").onclick = restart;

    document.getElementById("btnSend").onclick = async () => {
      const cmd = document.getElementById("cmd").value.trim();
      const payloadText = document.getElementById("payload").value.trim();
      let payload = null;
      if (payloadText) {
        try { payload = JSON.parse(payloadText); } catch (e) { alert("Bad JSON in payload"); return; }
      }
      try {
        const res = await api("/api/command", {method:"POST", body: JSON.stringify({command: cmd, payload})});
        alert("OK: " + JSON.stringify(res));
      } catch (e) {
        alert("Error: " + e.message);
      }
    };

    function connectLogs() {
      const logsDiv = document.getElementById("logs");
      const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/logs?token=" + encodeURIComponent(TOKEN));
      ws.onmessage = (ev) => {
        const atBottom = logsDiv.scrollTop + logsDiv.clientHeight >= logsDiv.scrollHeight - 5;
        logsDiv.textContent += ev.data + "\\n";
        if (atBottom) logsDiv.scrollTop = logsDiv.scrollHeight;
      };
      ws.onclose = () => setTimeout(connectLogs, 1500);
    }

    refreshStatus();
    setInterval(refreshStatus, 2000);
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
    return asyncio.create_task(server.serve())

# Позволяет запускать как отдельный процесс: uvicorn http_server:app --reload
# Перед этим нужно, чтобы attach() был вызван или чтобы оркестратор создавался здесь,
# но в большинстве случаев вы будете стартовать сервер из основного приложения.
