# core/ui.py

import sys
import io
import asyncio
from asyncio import Task
from typing import Optional, Callable, List, TextIO, IO, cast

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static
from textual.widgets import Placeholder
from textual.scroll_view import ScrollView
from textual.containers import Container

#_______________________________________________________________________________________


# -----------------------------
# Виджет логов с поддержкой \r
# -----------------------------
class LogPanel(ScrollView):
    """Панель логов, которая принимает сырые куски текста и честно отображает их."""

    def __init__(self, *, id: str = "log-panel"):
        super().__init__(id=id)
        self._buf: List[str] = []
        self._mounted_ready = False
        self._body = Static("", id="log-body")

    def on_mount(self):
        self._mounted_ready = True
        self.mount(self._body)

    def append_raw(self, chunk: str):
        """Добавить сырую строку (любой длины), соблюдая все символы, включая \\r."""
        if not isinstance(chunk, str):
            chunk = str(chunk)

        # Эмуляция поведения каретки: \r = "в начало строки"
        # Реализуем посимвольно, чтобы точно «до последнего знака».
        text = self._body.renderable or ""
        if not isinstance(text, str):  # на всякий случай
            text = str(text)

        for ch in chunk:
            if ch == "\r":
                # "Стереть" текущую строку, оставив всё до последней \n
                nl = text.rfind("\n")
                text = "" if nl == -1 else text[: nl + 1]
            else:
                text += ch

        self._body.update(text)
        # Доскроллить вниз без анимации (чтобы не лагало)
        self.scroll_end(animate=False)


# -----------------------------------------------
# Поток-перехватчик: дублирует запись в UI-панель
# -----------------------------------------------
class ConsoleTee(io.TextIOBase):
    def __init__(
        self,
        emit: Callable[[str], None],
        original: Optional[TextIO] = None,  # или Optional[IO[str]]
        mirror_to_terminal: bool = False,
    ):
        """
        emit: callback, который получит текст (из write).
        original: исходный поток (stdout/stderr), в который можно дополнительно писать.
        mirror_to_terminal: если True — дублировать в терминал.
        """
        super().__init__()
        self._emit = emit
        self._orig: Optional[TextIO] = original  # или Optional[IO[str]]
        self._mirror = mirror_to_terminal

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            try:
                s = s.decode(errors="replace")
            except Exception:
                s = str(s)

        # Передаём в лог-панель
        self._emit(s)

        # При необходимости — дублируем в оригинальный поток
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
# Основное приложение UI
# ----------------------
class TUIOrchestrator(App):
    """
    R — обновить статусы
    Q — выход
    Весь консольный вывод «вливается» в лог-панель сверху.
    """

    BINDINGS = [
        ("r", "refresh", "Обновить статусы"),
        ("q", "quit", "Выход"),
    ]

    CSS = """
    #log-container {
    height: 1fr;               /* Занять всё доступное пространство */
    overflow: auto;
    background: $surface;      /* чтобы не смешивалось визуально */
    }

    #log-panel {
    height: auto;              /* занимает высоту по содержимому */
    padding: 1 2;
    }

    #task-status {
    dock: bottom;
    height: auto;
    padding: 0 2;
    content-align: right middle;
    background: $panel;
    }
    """

    def __init__(self, orchestrator, *, mirror_console_to_terminal: bool = False):
        super().__init__()
        self.orchestrator = orchestrator

        # Виджеты
        self.status = TaskStatus(id="task-status")
        self.log_panel = LogPanel(id="log-panel")

        # Очередь на случай ранних write до монтирования UI
        self._pending_chunks: list[str] = []

        # Перехват stdout/stderr
        self._orig_stdout: TextIO = cast(TextIO, sys.stdout)  # или IO[str]
        self._orig_stderr: TextIO = cast(TextIO, sys.stderr)

        def emit_chunk(s: str):
            # Если вызов не из UI-потока — безопасно прокинем в UI
            try:
                self.call_from_thread(self._append_to_log_safe, s)
            except Exception:
                # До старта цикла / если UI еще не готов — буферизуем
                self._pending_chunks.append(s)

        sys.stdout = ConsoleTee(
            emit=emit_chunk, original=self._orig_stdout, mirror_to_terminal=mirror_console_to_terminal
        )
        sys.stderr = ConsoleTee(
            emit=emit_chunk, original=self._orig_stderr, mirror_to_terminal=mirror_console_to_terminal
        )

    def _append_to_log_safe(self, s: str):
        # Если лог уже смонтирован — пишем сразу, иначе буферизуем
        if getattr(self.log_panel, "_mounted_ready", False):
            self.log_panel.append_raw(s)
        else:
            self._pending_chunks.append(s)

    async def on_mount(self):
        # Догоним всё, что пришло раньше
        if self._pending_chunks:
            for s in self._pending_chunks:
                self.log_panel.append_raw(s)
            self._pending_chunks.clear()

        # Первичное обновление статусов
        await self.update_status()

        # Проверочный вывод (можно удалить)
        print("Лог-панель подключена к stdout/stderr ✅")

    async def on_unmount(self):
        # Восстановим системные потоки
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
        yield Footer()
        yield Container(
           self.log_panel,
            id="log-container"
        )
        yield self.status


async def run_async_ui(orchestrator):
    app = TUIOrchestrator(orchestrator)
    await app.run_async()