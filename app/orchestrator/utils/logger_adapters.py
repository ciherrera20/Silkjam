import logging

class ComposableLoggerAdapter(logging.LoggerAdapter):
    """
    Allows the following syntax:
        adapter1(extra1) | adapter2(extra2) | adapter3(extra3) | logger = adapter1(adapter2(adapter3(logger, extra3), extra2), extra1)
    """
    def __init__(self, logger=None, extra=None):
        if isinstance(logger, (logging.Logger, logging.LoggerAdapter)):
            self._glue = False
            super().__init__(logger, extra)
        else:
            self._adapters = [self]
            self._extra = logger
            self._glue = True

    def __or__(self, other):
        if self._glue and (isinstance(other, ComposableLoggerAdapter) and other._glue):
            self._adapters.append(other)
            return self
        elif self._glue and isinstance(other, (logging.Logger, logging.LoggerAdapter)):
            adapted = other
            for adapter in reversed(self._adapters):
                adapted = adapter.__class__(adapted, adapter._extra)
            return adapted
        else:
            raise NotImplementedError

    def __repr__(self):
        if self._glue:
            # return '<%s %s (%s)>' % (self.__class__.__name__, logger.name, level)
            s = f"{self.__class__.__name__}({repr(self._extra)})"
            for adapter in reversed(self._adapters[1:]):
                s += f" | {adapter.__class__.__name__}({repr(adapter._extra)})"
            return s
        else:
            return super().__repr__()

class PrefixLoggerAdapter(ComposableLoggerAdapter):
    """ A logger adapter that adds a prefix to every message """
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        return (f"[{self.extra}] {msg}", kwargs)

class BytesLoggerAdapter(ComposableLoggerAdapter):
    """ A logger adapter that takes bytes as messages and decodes them into strings """
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        return (msg.decode(self.extra or 'utf-8'), kwargs)