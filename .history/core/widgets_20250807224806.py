# core/widgets.py
import asyncio
from textual.widgets import Static

class TaskStatus(Static):
    """Виджет для отображения статуса asyncio-задач."""

    def update_status(self, task_map: dict[str, asyncio.Task | None]):
        lines = []
        for name, task in task_map.items():
            if task is None:
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