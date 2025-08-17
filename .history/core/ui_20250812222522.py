# core/ui.py

import sys
import io
import asyncio
import logging
from asyncio import Task
from typing import Optional, Callable, List, TextIO, IO, cast

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static
from textual.scroll_view import ScrollView
from textual.containers import Container

from core.logger import logger, formatter  # убедись, что эти объекты определены в logger.py

# -----------------------------
# Виджет логов с поддержкой \r
# -----------------------------
class LogPanel(ScrollView):
    def __init__(self, *, id: str = "log-panel"):
        super().__init__(id=id)
        self._buf: List[str] = []
        self._mounted_ready = False
        self._body = Static("", id="log-body")

    def on_mount(self):
        self._mounted_ready = True
        self.mount(self._body)

    def append_raw(self, chunk: str):
        if not isinstance(chunk, str):
            chunk = str(chunk)

        text = self._body.renderable or ""
        if not isinstance(text, str):
            text = str(text)

        for ch in chunk:
            if ch == "\r":
                nl = text.rfind("\n")
                text = "" if nl == -1 else text[: nl + 1]
            else:
                text += ch

        self._body.update(text)
        self.scroll_end(animate=False)


# -----------------------------------------------
# Поток-перехватчик: дублирует запись в UI-панель
# -----------------------------------------------
class ConsoleTee(io.TextIOBase):
    def __init__(
        self,
        emit: Callable[[str], None],
        original: Optional[TextIO] = None,
        mirror_to_terminal: bool = False,
    ):
        super().__init__()
        self._emit = emit
        self._orig: Optional[TextIO] = original
        self._mirror = mirror_to_terminal

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            try:
                s = s.decode(errors="replace")
            except Exception:
                s = str(s)

        self._emit(s)

        if self._mirror and self._orig is not None:
            try:
                self._orig.write(s)
                self._orig.flush()
            except Exception:
                pass

        return len(s)

    def flush(self):
        if self._mirror and self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass


# ----------------------
# Виджет статуса задач
# ----------------------
class TaskStatus(Static):
    def update_status(self, task_map: dict[str, Optional[Task]]):
        parts: list[str] = []
        for name, task in task_map.items():
            if task is None:
                status = "⚪ not started"
            elif task.cancelled():
                status = "❌ cancelled"
            elif task.done():
                exc = task.exception()
                status = f"💥 error: {exc}" if exc else "✅ done"
            else:
                status = "🟢 running"
            parts.append(f"{name}: {status}")
        line = " | ".join(parts) or "Нет активных задач"
        self.update(line)


# ----------------------
# Обработчик логов для UI
# ----------------------
class UILogHandler(logging.Handler):
    def __init__(self, emit: Callable[[str], None]):
        super().__init__()
        self._emit = emit
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._emit(msg + "\n")
        except Exception:
            self.handleError(record)


# ----------------------
# Основное приложение UI
# ----------------------
class TUIOrchestrator(App):
    BINDINGS = [
        ("r", "refresh", "Обновить статусы"),
        ("q", "quit", "Выход"),
    ]

    CSS = """
    #task-status {
    dock: bottom;
    height: auto;
    padding: 0 2;
    content-align: right middle;
    background: $panel;
    layer: overlay;
    }

    #log-panel {
    height: 33%;
    max-height: 40vh;
    min-height: 10vh;
    overflow-y: auto;
    padding: 1 2;
    background: $surface;
    }
    """

    def __init__(self, orchestrator, *, mirror_console_to_terminal: bool = False):
        super().__init__()
        self.orchestrator = orchestrator
        self.status = TaskStatus(id="task-status")
        self.log_panel = LogPanel(id="log-panel")
        self._pending_chunks: list[str] = []

        self._orig_stdout: TextIO = cast(TextIO, sys.stdout)
        self._orig_stderr: TextIO = cast(TextIO, sys.stderr)

        def emit_chunk(s: str):
            try:
                self.call_from_thread(self._append_to_log_safe, s)
            except Exception:
                self._pending_chunks.append(s)

        sys.stdout = ConsoleTee(
            emit=emit_chunk, original=self._orig_stdout, mirror_to_terminal=mirror_console_to_terminal
        )
        sys.stderr = ConsoleTee(
            emit=emit_chunk, original=self._orig_stderr, mirror_to_terminal=mirror_console_to_terminal
        )

        self._emit_chunk = emit_chunk

    def _append_to_log_safe(self, s: str):
        if getattr(self.log_panel, "_mounted_ready", False):
            self.log_panel.append_raw(s)
        else:
            self._pending_chunks.append(s)

    async def on_mount(self):
        import logging

        # 1) Хендлер только один раз
        if not getattr(self, "_ui_log_handler_added", False):
            
            root = logging.getLogger()            # root-логгер
            root.setLevel(logging.DEBUG)          # чтобы INFO точно проходил

            # Создаём и настраиваем UILogHandler
            ui_handler = UILogHandler(self._append_to_log_safe)
            ui_handler.setLevel(logging.DEBUG)
            ui_handler.setFormatter(logging.Formatter(
                fmt='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
                datefmt='%d-%m %H:%M:%S'
            ))

            # Не дублировать, если уже есть такой хендлер
            if not any(isinstance(h, UILogHandler) for h in root.handlers):
                root.addHandler(ui_handler)

            # Отключаем "протекание" для именованных логгеров при желании
            # logging.getLogger("bot").propagate = True  # root ловит всё; можно оставить True
            logging.getLogger("orchestrator").propagate = True
            self._ui_log_handler_added = True

            # Тестовая запись — должна появиться в UI
            logging.getLogger(__name__).info("UILogHandler подключён (root)")

        # 2) Проливаем отложенные куски в лог-панель
        if self._pending_chunks:
            for s in self._pending_chunks:
                self.log_panel.append_raw(s)
            self._pending_chunks.clear()

        # 3) Монтируем статус-строку внизу (если ещё не смонтирован)
        if not self.status.parent:
            await self.mount(self.status)

        # 4) Первая отрисовка статусов
        await self.update_status()


    async def on_unmount(self):
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr

    async def update_status(self):
        task_map = {
            "order_task": getattr(self.orchestrator, "order_task", None),
            "prehedge_task": getattr(self.orchestrator, "prehedge_task", None),
            "snapshot_task": getattr(self.orchestrator, "snapshot_task", None),
            "reinvest_task": getattr(self.orchestrator, "_reinvest_task", None),
        }
        self.status.update_status(task_map)

    async def action_refresh(self):
        await self.update_status()
        print("Статусы обновлены 🔄")

    def compose(self) -> ComposeResult:
        yield Header()
        yield self.log_panel
        yield Footer()
        #yield self.status


async def run_async_ui(orchestrator):
    app = TUIOrchestrator(orchestrator)
    await app.run_async()
