# core/ui.py
import asyncio
from asyncio import Task
from typing import Optional
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Button
from textual.containers import Container

class TaskStatus(Static):
    def update_status(self, task_map: dict[str, Optional[Task]]):
        lines = []
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
            lines.append(f"{name:<20} {status}")
        self.update("\n".join(lines) or "Нет активных задач")

class TUIOrchestrator(App):
    CSS_PATH = None  # можно добавить стили позже

    def __init__(self, orchestrator):
        super().__init__()
        self.orchestrator = orchestrator
        self.status_widget = TaskStatus()

    async def on_mount(self):
        await self.refresh_status()

    async def refresh_status(self):
        task_map = {
            "order_task": getattr(self.orchestrator, "order_task", None),
            "prehedge_task": getattr(self.orchestrator, "prehedge_task", None),
            "snapshot_task": getattr(self.orchestrator, "snapshot_task", None),
            "reinvest_task": getattr(self.orchestrator, "_reinvest_task", None),
        }
        self.status_widget.update_status(task_map)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            self.status_widget,
            Button("🔄 Обновить", id="refresh-btn"),
            Button("🚪 Выход", id="exit-btn")
        )
        yield Footer()

    async def on_button_pressed(self, event):
        if event.button.id == "refresh-btn":
            await self.refresh_status()
        elif event.button.id == "exit-btn":
            await self.action_quit()

async def run_async_ui(orchestrator):
    app = TUIOrchestrator(orchestrator)
    await app.run_async()
