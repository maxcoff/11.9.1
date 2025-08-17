# task_tracker.py
import asyncio

TRACKED_TASKS: dict[str, str] = {    
    "shutdown":	"завершение работы",
    "snapshot":	"Снапшот",
    "TPSL monitor":	"TPSL монитор",
    "ws_privat":	"WS соединение privat",
    "ws_public":	"WS соединение public",
    "orchestrator":	"Основной цикл",
    "reinvest loop":"Реинвест"
}

def add_tracked_task(name: str, description: str = ""):
    TRACKED_TASKS[name] = description

def create_tracked_task(coro, name: str, description: str = ""):
    if name not in TRACKED_TASKS:
        TRACKED_TASKS[name] = description
    return asyncio.create_task(coro, name=name)

def list_tracked_tasks():
    """
    Возвращает список словарей с полями:
    - name: системное имя задачи
    - description: описание
    - running: bool, запущена ли задача сейчас
    """
    result = []
    for name, desc in TRACKED_TASKS.items():
        running = any(t.get_name() == name and not t.done() for t in asyncio.all_tasks())
        result.append({
            "name": name,
            "description": desc,
            "running": running
        })
    return result
