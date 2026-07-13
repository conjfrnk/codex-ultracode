class EventRouter:
    """Map canonical event names to handlers.

    Registering a canonically equivalent name replaces the previous handler.
    Dispatching an unknown event raises ``KeyError``.
    """

    def __init__(self):
        self._handlers = {}

    def register(self, name: str, handler) -> None:
        key = name.strip().lower().replace("_", ".")
        self._handlers[key] = handler

    def dispatch(self, name: str, payload):
        key = name.lower()
        return self._handlers[key](payload)
