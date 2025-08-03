# logger.py

import os
import uuid
import logging
import time
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime

from core.config import get

# 1) Логи всегда в UTC+3 (независимо от системной TZ)
def _gmtime_plus3(sec=None):
    """
    Принимает timestamp (sec) и возвращает структуру времени GMT+3.
    Если sec не указан, берёт текущее время.
    """
    ts = sec if sec is not None else time.time()
    return time.gmtime(ts + 3 * 3600)

# Назначаем converter как staticmethod, чтобы не было привязки к экземпляру Formatter
logging.Formatter.converter = staticmethod(_gmtime_plus3)

# 2) Папка и сессия
os.makedirs("logs", exist_ok=True)
SESSION_ID = uuid.uuid4().hex

# 3) Фильтр контекста
class ContextFilter(logging.Filter):
    instrument = get("INSTRUMENT") or "-"
    session = SESSION_ID

    def filter(self, record: logging.LogRecord) -> bool:
        record.instrument = self.instrument
        record.session = self.session
        record.mode = getattr(record, "mode", "-")
        record.errorCode = getattr(record, "errorCode", "-")
        return True

# 4) Кастомный хендлер с DD-MM-YY
class CustomTRFH(TimedRotatingFileHandler):
    def __init__(self, filename, **kwargs):
        super().__init__(filename, **kwargs)
        base, ext = os.path.splitext(self.baseFilename)
        self.base, self.ext = base, ext

    def rotation_filename(self, default_name: str) -> str:
        # default_name: logs/bot.log.YYYY-MM-DD
        date_part = default_name.rsplit(".", 1)[-1]
        ddmmyy = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%m-%y")
        return f"{self.base}_{ddmmyy}{self.ext}"

# 5) Формат и formatter
_fmt = (
    "[%(asctime)s] %(levelname)s %(name)s "
    "[inst=%(instrument)s] [sess=%(session)s] "
    "[mode=%(mode)s] [err=%(errorCode)s]: %(message)s"
)
formatter = logging.Formatter(
    _fmt,
    datefmt="%Y-%m-%dT%H:%M:%S"
)

# 6) Настройка логгера
logger = logging.getLogger("bot")
logger.setLevel(logging.DEBUG)
logger.addFilter(ContextFilter())

# 7) StreamHandler (консоль)
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
console.setFormatter(formatter)
logger.addHandler(console)

# 8) FileHandlers
for name, level in (("bot.log", logging.DEBUG), ("error.log", logging.ERROR)):
    fh = CustomTRFH(
        filename=f"logs/{name}",
        when="midnight",
        backupCount=7,
        encoding="utf-8"
    )
    fh.setLevel(level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
