"""Request-local streaming observations; no global cross-conversation callback."""

from contextvars import ContextVar

observer = ContextVar("carlos_reply_observer", default=None)


def emit(kind, text="", latency_ms=None):
    callback = observer.get()
    if callback is not None:
        callback(kind, text, latency_ms)
