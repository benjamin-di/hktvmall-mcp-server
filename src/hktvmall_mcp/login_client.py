"""Route fixed default recovery to its custodian without exporting credentials."""

from signin_window.client import LoginUnavailable


class RoutedLoginClient:
    PREFIX = "m_"
    NORMAL_PREFIX = "l_"

    def __init__(self, normal, managed):
        self.normal, self.managed = normal, managed

    def _selected(self, window_id):
        if window_id.startswith(self.PREFIX):
            if self.managed is None:
                raise LoginUnavailable("default recovery unavailable")
            return self.managed, window_id[len(self.PREFIX) :]
        if window_id.startswith(self.NORMAL_PREFIX):
            return self.normal, window_id[len(self.NORMAL_PREFIX) :]
        raise LoginUnavailable("invalid recovery window")

    def start(self, layout="desktop", **params):
        managed = params.get("key") == "default"
        backend = self.managed if managed else self.normal
        if backend is None:
            raise LoginUnavailable("default recovery unavailable")
        window = backend.start(layout, **params)
        prefix = self.PREFIX if managed else self.NORMAL_PREFIX
        window = dict(window, id=prefix + window["id"])
        if len(window["id"]) > 40:
            backend.close(window["id"][len(prefix) :])
            raise LoginUnavailable("invalid recovery window")
        return window

    def state(self, window_id):
        backend, raw = self._selected(window_id)
        return backend.state(raw)

    def result(self, window_id):
        backend, raw = self._selected(window_id)
        return None if window_id.startswith(self.PREFIX) else backend.result(raw)

    def close(self, window_id):
        backend, raw = self._selected(window_id)
        return backend.close(raw)

    def static(self, path, window_id=None):
        backend, raw = self._selected(window_id) if window_id else (self.normal, None)
        return backend.static(path, raw)

    async def bridge(self, window_id, view_token, ws):
        backend, raw = self._selected(window_id)
        return await backend.bridge(raw, view_token, ws)
