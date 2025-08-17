# core/task_tracker.py
import asyncio
from core.http_server import broadcaster  # твой LogBroadcaster или другой менеджер WS

TASK_STATE = {}

async def push_task_state(task_name, state):
    TASK_STATE[task_name] = state
    await broadcaster.push(f"TASKSTATE:{task_name}:{state}")


ALL_TASKS = set()

def create_tracked_task(coro, name: str):
    task = asyncio.create_task(coro, name=name)
    ALL_TASKS.add(name)
    return task

def list_tracked_tasks():
    tasks_info = []
    for t in asyncio.all_tasks():
        if t.get_name() in ALL_TASKS:
            state = "finished" if t.done() else "running"
            tasks_info.append((t.get_name(), state))
    return tasks_info