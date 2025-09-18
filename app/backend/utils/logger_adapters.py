import logging

class PrefixLoggerAdapter(logging.LoggerAdapter):
    """ A logger adapter that adds a prefix to every message """
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        prefix = " ".join(f"[{val}]" for val in self.extra.values())
        return super().process(f"{prefix} {msg}", kwargs)