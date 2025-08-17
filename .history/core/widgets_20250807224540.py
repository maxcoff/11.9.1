import logging
from textual.widgets import Log

class WidgetHandler(logging.Handler):
    def __init__(self, widget: Log) -> None:
        super().__init__()
        self.widget = widget
        # задаём формат через стандартный Formatter, чтобы не ломать %H
        self.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # пушим в Log-виджет построчно
            for line in msg.splitlines():
                self.widget.write(line)
        except Exception:
            self.handleError(record)