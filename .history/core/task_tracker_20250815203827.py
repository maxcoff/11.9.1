# core/task_tracker.py
import asyncio
from core.http_server import broadcaster  # твой LogBroadcaster или другой менеджер WS

TASK_STATE = {}

async def push_task_state(task_name, state):
    TASK_STATE[task_name] = state
    await broadcaster.push(f"TASKSTATE:{task_name}:{state}")

def create_tracked_task(coro, name):
    task = asyncio.create_task(coro, name=name)
    asyncio.create_task(push_task_state(name, "running"))

    def _done_callback(t: asyncio.Task):
        if t.cancelled():
            state = "cancelled"
        elif t.exception():
            state = "error"
        else:
            state = "finished"
        asyncio.create_task(push_task_state(name, state))

    task.add_done_callback(_done_callback)
    return task
