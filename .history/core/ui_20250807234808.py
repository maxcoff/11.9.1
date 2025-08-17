# core/ui.py

import sys
import asyncio
from asyncio import Task
from typing import Optional

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Button
from textual.containers import Container


class TaskStatus(Static):
    def update_status(self, task_map: dict[str, Optional[Task]]):
        lines: list[str] = []
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
    BINDINGS = [
        ("ctrl+u", "toggle_ui", "Скрыть/Показать UI"),
        ("q", "quit", "Выход"),
    ]

    CSS_PATH = None  # можно добавить стили позже

    def __init__(self, orchestrator):
        super().__init__()
        self.orchestrator = orchestrator
        self.status = TaskStatus()
        self.ui_visible = True

    async def on_mount(self):
        await self.update_status_task()
        self.set_interval(5, self.update_status_task)

    async def update_status_task(self):
        task_map = {
            "order_task":     getattr(self.orchestrator, "order_task",     None),
            "prehedge_task":  getattr(self.orchestrator, "prehedge_task",  None),
            "snapshot_task":  getattr(self.orchestrator, "snapshot_task",  None),
            "reinvest_task":  getattr(self.orchestrator, "_reinvest_task",  None),
            # добавьте остальные задачи по аналогии
        }
        self.status.update_status(task_map)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            self.status,
            Button("🔄 Обновить", id="refresh-btn"),
            Button("🚪 Выход", id="exit-btn"),
        )
        yield Footer()

    async def on_button_pressed(self, event):
        if event.button.id == "refresh-btn":
            await self.update_status_task()
        elif event.button.id == "exit-btn":
            await self.action_quit()

    def exit_alternate_screen(self) -> None:
        """
        Выходим из альтернативного экрана (-1049l ESC),
        возвращаемся в обычный буфер терминала.
        """
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()

    def enter_alternate_screen(self) -> None:
        """
        Входим в альтернативный экран (-1049h ESC),
        включаем Textual UI обратно.
        """
        sys.stdout.write("\x1b[?1049h")
        sys.stdout.flush()

    async def action_toggle_ui(self):
        """
        Хоткей Ctrl+U: скрыть или показать TUI.
        """
        if self.ui_visible:
            self.exit_alternate_screen()
            print("🔧 UI отключён. Логи и print() снова видны.")
        else:
            self.enter_alternate_screen()
            # обновляем и перерисовываем содержимое
            await self.update_status_task()
            self.refresh()
        self.ui_visible = not self.ui_visible


async def run_async_ui(orchestrator):
    app = TUIOrchestrator(orchestrator)
    await app.run_async()
