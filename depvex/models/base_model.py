import os

class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

    @classmethod
    def enabled(cls) -> bool:
        return os.getenv("NO_COLOR") is None and os.getenv("TERM") not in {None, "dumb"}

    @classmethod
    def colorize(cls, text: str, color: str) -> str:
        return f"{color}{text}{cls.RESET}" if cls.enabled() else text