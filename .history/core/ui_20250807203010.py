# ui.py
import textual as tx


from textual.app import App
from textual.events import Event    
from textual.widgets import Button, Static, Label

class TradingUI(tx.App):
    def compose(self) -> tx.Compositor:
        yield tx.Label("TPSL Monitor State")

    def on_mount(self) -> None:
        self.update_ui()

    def update_ui(self) -> None:
        self.query_one("Label").update("TPSL Monitor State: RUNNING")

if __name__ == "__main__":
    TradingUI.run()