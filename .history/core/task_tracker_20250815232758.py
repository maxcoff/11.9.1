# core/task_tracker.py
import asyncio
from core.http_server import broadcaster  # твой LogBroadcaster или другой менеджер WS

TASK_STATE = {}

async def push_task_state(task_name, state):
    TASK_STATE[task_name] = state
    await broadcaster.push(f"TASKSTATE:{task_name}:{state}")


# Словарь: имя_таски -> описание/назначение (по желанию)
TRACKED_TASKS = {
    "fetch_users": "Загрузка пользователей из БД",
    "sync_orders": "Синхронизация заказов",
    "send_emails": "Рассылка писем"
}

def create_tracked_task(coro, name: str):
    task = asyncio.create_task(coro, name=name)
    ALL_TASKS.add(name)
    return task

def add_tracked_task(name: str, description: str = ""):
    """Добавить новое имя таски в трекер на лету."""
    TRACKED_TASKS[name] = description

def list_tracked_tasks():
    tasks_info = []
    all_running = asyncio.all_tasks()

    for name in TRACKED_TASKS:
        task = next((t for t in all_running if t.get_name() == name), None)
        if task:
            state = "finished" if task.done() else "running"
        else:
            state = "not_started"
        tasks_info.append((name, state))
    return tasks_info