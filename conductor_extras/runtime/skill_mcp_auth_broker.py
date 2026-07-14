import http.client
import json
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from .errors import ValidationError


MAX_SKILL_MCP_BROKER_REQUEST_BYTES = 8 * 1024 * 1024
MAX_SKILL_MCP_BROKER_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_SKILL_MCP_BROKER_RESPONSE_HEADER_BYTES = 8 * 1024
SKILL_MCP_BROKER_CONNECT_TIMEOUT_SECONDS = 10
SKILL_MCP_BROKER_RESPONSE_TIMEOUT_SECONDS = 65
MAX_SKILL_MCP_BROKER_RESPONSE_TIMEOUT_SECONDS = 24 * 60 * 60

_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
    "user-agent",
}
_RESPONSE_HEADERS = {
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
    "retry-after",
}
_BLOCKED_REQUEST_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class SkillMcpAuthBroker:
    def __init__(
        self,
        routes: Iterable[Mapping],
        *,
        connection_factory: Optional[Callable] = None,
        response_timeout_seconds: int = SKILL_MCP_BROKER_RESPONSE_TIMEOUT_SECONDS,
    ):
        cleaned = list(routes)
        if not cleaned:
            raise ValidationError("Skill MCP auth broker requires at least one route")
        if (
            not isinstance(response_timeout_seconds, int)
            or isinstance(response_timeout_seconds, bool)
            or response_timeout_seconds < 1
            or response_timeout_seconds > MAX_SKILL_MCP_BROKER_RESPONSE_TIMEOUT_SECONDS
        ):
            raise ValidationError("Skill MCP auth broker response timeout is invalid")
        self._state = _BrokerState(
            cleaned,
            connection_factory=connection_factory,
            response_timeout_seconds=response_timeout_seconds,
        )
        self._server = _BrokerServer(("127.0.0.1", 0), _BrokerHandler)
        self._server.broker_state = self._state
        port = self._server.server_address[1]
        self.urls = {
            route["name"]: "http://127.0.0.1:%d%s" % (port, path)
            for path, route in self._state.routes.items()
        }
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="conductor-skill-mcp-auth-broker",
            daemon=True,
        )
        self._closed = False
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._state.stop()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        self._state.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class _BrokerState:
    def __init__(
        self,
        routes,
        *,
        connection_factory=None,
        response_timeout_seconds: int,
    ):
        self.routes = {}
        seen_names = set()
        for route in routes:
            if not isinstance(route, Mapping) or set(route) != {
                "name",
                "url",
                "headers",
                "secret_values",
            }:
                raise ValidationError("Skill MCP auth broker route is invalid")
            name = route["name"]
            if not isinstance(name, str) or not name or name in seen_names:
                raise ValidationError("Skill MCP auth broker route name is invalid")
            seen_names.add(name)
            try:
                parsed = urlsplit(route["url"])
                port = parsed.port
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    "Skill MCP auth broker target must be pinned HTTPS"
                ) from exc
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)
                or any(char.isspace() for char in route["url"])
            ):
                raise ValidationError("Skill MCP auth broker target must be pinned HTTPS")
            headers = route["headers"]
            secret_values = route["secret_values"]
            if not isinstance(headers, Mapping) or not headers:
                raise ValidationError("Skill MCP auth broker route requires headers")
            if not isinstance(secret_values, tuple) or not secret_values:
                raise ValidationError("Skill MCP auth broker route requires secret values")
            path = "/mcp/%s" % secrets.token_urlsafe(24)
            self.routes[path] = {
                "name": name,
                "hostname": parsed.hostname,
                "port": port or 443,
                "target": parsed.path or "/",
                "headers": dict(headers),
                "redactor": _SecretByteRedactor(secret_values),
            }
        self.connection_factory = connection_factory or _https_connection
        self.response_timeout_seconds = response_timeout_seconds
        self.stop_event = threading.Event()
        self._connections = set()
        self._client_connections = set()
        self._lock = threading.Lock()

    def register(self, connection) -> None:
        with self._lock:
            if self.stop_event.is_set():
                raise OSError("broker stopped")
            self._connections.add(connection)

    def unregister(self, connection) -> None:
        with self._lock:
            self._connections.discard(connection)

    def register_client(self, connection) -> None:
        with self._lock:
            if self.stop_event.is_set():
                try:
                    connection.close()
                except Exception:
                    pass
                return
            self._client_connections.add(connection)

    def unregister_client(self, connection) -> None:
        with self._lock:
            self._client_connections.discard(connection)

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            connections = list(self._connections | self._client_connections)
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass

    def clear(self) -> None:
        with self._lock:
            self._connections.clear()
            self._client_connections.clear()
            self.routes.clear()


class _BrokerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _BrokerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ConductorSkillMcpBroker"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(SKILL_MCP_BROKER_CONNECT_TIMEOUT_SECONDS)
        self.server.broker_state.register_client(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.broker_state.unregister_client(self.connection)

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    def do_DELETE(self):
        self._forward("DELETE")

    def do_OPTIONS(self):
        self._fail(405)

    def do_PUT(self):
        self._fail(405)

    def do_PATCH(self):
        self._fail(405)

    def do_HEAD(self):
        self._fail(405)

    def do_CONNECT(self):
        self._fail(405)

    def do_TRACE(self):
        self._fail(405)

    def log_message(self, format, *args):
        del format, args

    def _forward(self, method: str) -> None:
        state = self.server.broker_state
        route = state.routes.get(self.path)
        if route is None or state.stop_event.is_set():
            self._fail(404)
            return
        try:
            body = self._request_body()
        except (OSError, ValidationError):
            self._fail(400)
            return
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() in _REQUEST_HEADERS
            and name.lower() not in _BLOCKED_REQUEST_HEADERS
        }
        headers.update(route["headers"])
        connection = None
        try:
            connection = state.connection_factory(
                route["hostname"],
                route["port"],
                SKILL_MCP_BROKER_CONNECT_TIMEOUT_SECONDS,
                state.response_timeout_seconds,
            )
            state.register(connection)
            connection.request(method, route["target"], body=body, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400 or response.status in {401, 403}:
                self._fail(502)
                return
            response_headers = self._response_headers(response, route["redactor"])
            content_type = response.getheader("Content-Type", "")
            if content_type.lower().startswith("text/event-stream"):
                self._stream_response(
                    response,
                    route["redactor"],
                    response_headers,
                )
            else:
                self._buffered_response(
                    response,
                    route["redactor"],
                    response_headers,
                )
        except Exception:
            # Keep endpoint, TLS, and credential-bearing transport failures out of
            # the provider-visible response and the HTTP server's default traceback.
            self._fail(502)
        finally:
            if connection is not None:
                state.unregister(connection)
                try:
                    connection.close()
                except Exception:
                    pass

    def _request_body(self) -> Optional[bytes]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise ValidationError("chunked broker requests are unsupported")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValidationError("invalid broker request length") from exc
        if length < 0 or length > MAX_SKILL_MCP_BROKER_REQUEST_BYTES:
            raise ValidationError("broker request exceeds its limit")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValidationError("broker request body is incomplete")
        return body

    def _buffered_response(self, response, redactor, response_headers) -> None:
        raw = response.read(MAX_SKILL_MCP_BROKER_RESPONSE_BYTES + 1)
        if len(raw) > MAX_SKILL_MCP_BROKER_RESPONSE_BYTES:
            self._fail(502)
            return
        body = redactor.replace(raw)
        self.send_response(response.status)
        self._write_response_headers(response_headers)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def _stream_response(self, response, redactor, response_headers) -> None:
        self.send_response(response.status)
        self._write_response_headers(response_headers)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        stream = redactor.stream()
        total = 0
        while not self.server.broker_state.stop_event.is_set():
            chunk = response.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SKILL_MCP_BROKER_RESPONSE_BYTES:
                break
            output = stream.feed(chunk)
            if output:
                self.wfile.write(output)
                self.wfile.flush()
        tail = stream.finish()
        if tail and total <= MAX_SKILL_MCP_BROKER_RESPONSE_BYTES:
            self.wfile.write(tail)
            self.wfile.flush()
        self.close_connection = True

    def _response_headers(self, response, redactor):
        cleaned = []
        for name, value in response.getheaders():
            if name.lower() in _RESPONSE_HEADERS:
                if not isinstance(value, str):
                    raise ValidationError("authenticated MCP response header is invalid")
                raw = value.encode("latin-1")
                if len(raw) > MAX_SKILL_MCP_BROKER_RESPONSE_HEADER_BYTES:
                    raise ValidationError("authenticated MCP response header exceeds its limit")
                value = redactor.replace(raw).decode("latin-1")
                if any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise ValidationError("authenticated MCP response header is unsafe")
                cleaned.append((name, value))
        return cleaned

    def _write_response_headers(self, response_headers) -> None:
        for name, value in response_headers:
            self.send_header(name, value)

    def _fail(self, status: int) -> None:
        body = b"Conductor authenticated MCP broker rejected the request.\n"
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True


class _SecretByteRedactor:
    def __init__(self, values):
        patterns = set()
        for value in values:
            if not isinstance(value, str) or not value:
                raise ValidationError("Skill MCP auth broker secret is invalid")
            patterns.add(value.encode("utf-8"))
            escaped = json.dumps(value, ensure_ascii=True)[1:-1].encode("ascii")
            patterns.add(escaped)
        self.patterns = tuple(sorted(patterns, key=lambda value: (-len(value), value)))
        self.max_pattern_bytes = max(len(value) for value in self.patterns)

    def replace(self, value: bytes) -> bytes:
        output = value
        for pattern in self.patterns:
            output = output.replace(pattern, b"<redacted>")
        return output

    def stream(self):
        return _StreamingSecretRedactor(self)


class _StreamingSecretRedactor:
    def __init__(self, redactor: _SecretByteRedactor):
        self.redactor = redactor
        self.tail = b""

    def feed(self, chunk: bytes) -> bytes:
        combined = self.tail + chunk
        keep = self.redactor.max_pattern_bytes - 1
        if keep <= 0 or len(combined) <= keep:
            self.tail = combined
            return b""
        boundary = len(combined) - keep
        for pattern in self.redactor.patterns:
            start = combined.find(pattern)
            while start >= 0:
                if start < boundary < start + len(pattern):
                    boundary = start
                    break
                start = combined.find(pattern, start + 1)
        emit = combined[:boundary]
        self.tail = combined[boundary:]
        return self.redactor.replace(emit)

    def finish(self) -> bytes:
        output = self.redactor.replace(self.tail)
        self.tail = b""
        return output


def _https_connection(
    hostname: str,
    port: int,
    connect_timeout: int,
    response_timeout: int,
):
    connection = http.client.HTTPSConnection(
        hostname,
        port,
        timeout=connect_timeout,
        context=ssl.create_default_context(),
    )
    connection.connect()
    if connection.sock is None:
        raise OSError("authenticated MCP broker connection is unavailable")
    connection.sock.settimeout(response_timeout)
    return connection
