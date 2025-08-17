import asyncio
import logging
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Button, Log
from textual.containers import Container

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
        handler = TextualLogHandler(self.log_panel)
        root = logging.getLogger()
        for handler in root.handlers:
            # Собираем корректный шаблон: время + сообщение
            fmt = "[%(asctime)s] %(message)s"
            handler.setFormatter(
                logging.Formatter(fmt, datefmt="%H:%M:%S")
            )
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

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
