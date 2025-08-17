import asyncio
import logging
from datetime import datetime, timezone

from core.http_server import start_http_server, state  # берем state из http_server

# --- Простейший "оркестратор" для теста ---
class Orchestrator:
    def __init__(self):
        self._counter = 0
        self.running = False
        self.start_time = None
        self.logger = logging.getLogger("bot")

    async def run(self):
        self.running = True
        self.start_time = datetime.now(timezone.utc)
        self.logger.info("Оркестратор запущен (демо)")
        try:
            while self.running:
                self._counter += 1
                self.logger.info(f"Счётчик активности: {self._counter}")
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.logger.warning("Демо оркестратор отменён")
        finally:
            self.running = False

    async def _shutdown(self):
        self.logger.info("Остановка демо оркестратора")
        self.running = False

    def get_status(self) -> dict:
        return {
            "demo_counter": self._counter,
            "running": self.running
        }

# --- Основной запуск ---
async def main():
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")

    orch = Orchestrator()

    # Запускаем HTTP-сервер в фоне
    await start_http_server(
        orch,
        logger=orch.logger,
        host="127.0.0.1",
        port=8000,
        admin_token="секрет"
    )

    # Запускаем оркестратор
    await orch.run()

if __name__ == "__main__":
    asyncio.run(main())
