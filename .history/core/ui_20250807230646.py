import asyncio
import logging
import sys
import traceback
from textual.app import App, ComposeResult, events
from textual.widgets import Header, Footer, Static, Button, Log
from textual.containers import Container
from textual import events


class TaskStatus(Static):
    def update_status(self, task_map: dict[str, asyncio.Task | None]):
        lines = []
        for name, task in task_map.items():
            if not task:
                status = "⚪ not started"
            elif task.cancelled():
                status = "❌ cancelled"
            elif task.done():
                exc = task.exception()
                status = f"💥 error: {exc!r}" if exc else "✅ done"
            else:
                status = "🟢 running"
            lines.append(f"{name:<20} {status}")
        self.update("\n".join(lines) or "Нет задач")

class TextualLogHandler(logging.Handler):
    """Кастомный лог-хэндлер, пишущий в TextLog."""
    def __init__(self, text_log: Log):
        super().__init__()
        self.text_log = text_log

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        # пишем в TextLog, TextLog.take_focus() не нужен
        self.text_log.write(msg)

class WidgetHandler(logging.Handler):
    def __init__(self, widget: Log) -> None:
        super().__init__()
        self.widget = widget
        # задаём формат через стандартный Formatter, чтобы не ломать %H
        self.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # пушим в Log-виджет построчно
            for line in msg.splitlines():
                self.widget.write(line)
        except Exception:
            self.handleError(record)

class TUIOrchestrator(App):
    TITLE = "Orchestrator TUI"
    BINDINGS = [
        ("q", "quit", "Выход"),
    ]

    CSS = """
    #status-container {
        height: 3fr;
        padding: 1;
    }
    TextLog {
        height: 5fr;
        border: round yellow;
        padding: 1;
    }
    #buttons {
        height: 1fr;
        padding: 1;
    }
    """

    def __init__(self, orchestrator):
        super().__init__()
        self.orchestrator = orchestrator
        self.status = TaskStatus(id="status-container")
        self.log_panel = Log(highlight=True)
        self.widget_handler = WidgetHandler(self.log_panel)

    def compose(self) -> ComposeResult:
        yield Header()
        yield self.status
        yield self.log_panel
        yield Container(
            Button("🔄 Обновить", id="refresh-btn"),
            Button("🚪 Выход",   id="exit-btn"),
            id="buttons"
        )
        yield Footer()

    async def on_mount(self):
        # 1) сразу обновляем статус
        await self.update_status_task()

        # 2) каждые 5 сек обновляем статус
        self.set_interval(5, self.update_status_task)

        # 3) подтягиваем логи внутрь TextLog
        h = TextualLogHandler(self.log_panel)
        root = logging.getLogger()
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler):
                root.removeHandler(h)
           
        
        root.addHandler(self.widget_handler)
        root.setLevel(logging.DEBUG)
        sys.excepthook = self._excepthook
    
    def _excepthook(self, exc_type, exc_value, exc_tb):
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        for line in tb_lines:
            # выводим каждый line как запись ERROR уровня
            record = logging.LogRecord(
                name="EXCEPTION",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg=line.rstrip(),
                args=(),
                exc_info=None,
            )
            self.widget_handler.emit(record)
    
    

    async def update_status_task(self):
        task_map = {
            "order_task":     getattr(self.orchestrator, "order_task",    None),
            "prehedge_task":  getattr(self.orchestrator, "prehedge_task", None),
            "snapshot_task":  getattr(self.orchestrator, "snapshot_task", None),
            "reinvest_task":  getattr(self.orchestrator, "_reinvest_task",None),
            # добавьте остальные
        }
        self.status.update_status(task_map)

    async def on_button_pressed(self, event):
        if event.button.id == "refresh-btn":
            await self.update_status_task()
        elif event.button.id == "exit-btn":
            await self.action_quit()

async def run_async_ui(orchestrator):
    app = TUIOrchestrator(orchestrator)
    await app.run_async()
