# core/ui.py

import sys
import io
import asyncio
from asyncio import Task
from typing import Optional, Callable, List

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static
from textual.widgets import Placeholder
from textual.scroll_view import ScrollView

#_______________________________________________________________________________________


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


class TUIOrchestrator(App):
    """
    R — обновить статусы  
    Q — выход  
    Статусы отображаются одной строкой в правом нижнем углу
    """
    BINDINGS = [
        ("r", "refresh", "Обновить статусы"),
        ("q", "quit",    "Выход"),
    ]
    
    CSS = """
    #task-status {
      dock: bottom;
      height: auto;
      padding: 0 2;
      content-align: right middle;
      background: $panel;
    }
    """

    def __init__(self, orchestrator):
        super().__init__()
        self.orchestrator = orchestrator
        self.status = TaskStatus(id="task-status")

    async def on_mount(self):
        # Однократное обновление при старте
        await self.update_status()
        

    async def update_status(self):
        task_map = {
            "order_task":    getattr(self.orchestrator, "order_task",    None),
            "prehedge_task": getattr(self.orchestrator, "prehedge_task", None),
            "snapshot_task": getattr(self.orchestrator, "snapshot_task", None),
            "reinvest_task": getattr(self.orchestrator, "_reinvest_task", None),
            # добавьте остальные задачи по аналогии
        }
        self.status.update_status(task_map)

    async def action_refresh(self):
        """
        Обработка нажатия R
        """
        await self.update_status()

    def compose(self) -> ComposeResult:
        # Только шапка и подвал, статус — отдельным доком
        yield Header()
        yield Footer()
        yield self.status


async def run_async_ui(orchestrator):
    app = TUIOrchestrator(orchestrator)
    await app.run_async()
