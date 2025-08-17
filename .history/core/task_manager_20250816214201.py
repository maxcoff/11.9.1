# task_manager.py
import asyncio
from typing import Callable, Awaitable, Optional, Any, TypeVar, Coroutine
from core.http_server import broadcaster
from core.logger            import logger

T = TypeVar("T")
class TaskManager:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None        
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._factories: dict[str, Callable[[], Awaitable]] = {}
        self._descriptions: dict[str, str] = {}          
        self._listeners: list[Callable[[], None]] = []  # колбэки при изменении

    def _ensure_loop(self):        
        if self._loop is None:
            try:                
                self._loop = asyncio.get_running_loop()
            except RuntimeError:                
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)

    def _broadcast_state(self, name: str):
        # получаем актуальное состояние через list()
        state = next((t["state"] for t in self.list() if t["name"] == name), "unknown")
        asyncio.create_task(broadcaster.push(f"TASKSTATE:{name}:{state}"))
    
    def create(self, name: str, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Создаёт и регистрирует задачу."""
        if name in self._tasks:
            raise ValueError(f"Задача с именем {name} уже существует")
        task = asyncio.create_task(coro, name=name)
        self._tasks[name] = task
        return task
    
    def get(self, name: str) -> asyncio.Task[Any] | None:
        return self._tasks.get(name)
    
    def cancel(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
    
    def _notify(self):
        for cb in self._listeners:
            try:
                cb()
            except Exception as e:
                print(f"[TaskManager] listener error: {e}")
        
        # регистрация
    def register(self, name: str, description: str, factory: Callable[[], Awaitable]):
        self._descriptions[name] = description
        self._factories[name] = factory
        #Подписка на обновления
    def on_change(self, callback: Callable[[], None]):        
        self._listeners.append(callback)

    def start(self, name: str) -> bool:
        self._ensure_loop()
        if name in self._tasks and not self._tasks[name].done():
            return False
        if name not in self._factories:
            raise ValueError(f"Нет фабрики для задачи {name}")
        task = self._loop.create_task(self._factories[name](), name=name) # type: ignore 
        self._tasks[name] = task
        self._broadcast_state(name)
        task.add_done_callback(lambda _: self._broadcast_state(name))
        #self._notify()
        return True

    def stop(self, name: str) -> bool:
        task = self._tasks.get(name)
        if task and not task.done():
            task.cancel()
            self._notify()
            return True
        return False

    def list(self):
        """Список задач с состояниями для панели"""
        result = []
        for name, desc in self._descriptions.items():
            task = self._tasks.get(name)
            if task:
                if task.cancelled():
                    state = "cancelled"
                elif task.done():
                    state = "finished"
                else:
                    state = "running"
            else:
                state = "not_started"
            result.append({
                "name": name,
                "description": desc,
                "state": state
            })
            print(result)
        return result

    async def wait(self, name: str):            
            task = self._tasks.get(name)
            if task:
                await task
    

    async def wait_all(self):
        """Дождаться завершения всех задач."""
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def cancel_all(self):
        """
        Асинхронно отменяет все задачи и дожидается их завершения.
        """        
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
                self._broadcast_state(name)
                logger.info(f"🛑 Отмена задачи '{name}'", extra={"mode": "TASK_MANAGER"})
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

            
           
            try:
                await task
            except asyncio.CancelledError:
                logger.debug(f"Задача '{name}' завершена с CancelledError", extra={"mode": "TASK_MANAGER"})
            except Exception as e:
                logger.warning(f"Задача '{name}' завершилась с исключением: {e}", extra={"mode": "TASK_MANAGER"})

        # после завершения — очищаем словарь
        self._tasks.clear()
        logger.info("✅ Все задачи успешно отменены", extra={"mode": "TASK_MANAGER"})