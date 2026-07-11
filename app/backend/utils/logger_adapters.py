import logging
from collections.abc import MutableMapping
from typing import Any


class PrefixLoggerAdapter(logging.LoggerAdapter[Any]):
    """ A logger adapter that adds a prefix to every message """
    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        prefix = " ".join(f"[{val}]" for val in self.extra.values()) + " " if self.extra is not None else ""
        return super().process(f"{prefix}{msg}", kwargs)