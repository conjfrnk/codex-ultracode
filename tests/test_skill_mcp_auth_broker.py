import http.client
import json
import socket
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

from conductor_runtime.agent_skill_mcp import codex_skill_mcp_config_arg
from conductor_runtime.errors import ValidationError
from conductor_runtime.skill_mcp_auth_broker import SkillMcpAuthBroker


class FakeResponse:
    def __init__(
        self,
        body,
        *,
        status=200,
        content_type="application/json",
        chunks=None,
        extra_headers=None,
    ):
        self.status = status
        self._body = body
        self._chunks = list(chunks or [])
        self._headers = [
            ("Content-Type", content_type),
            ("Mcp-Session-Id", "session-123"),
            ("Set-Cookie", "must-not-forward=true"),
            ("Location", "https://attacker.invalid/redirect"),
        ]
        self._headers.extend(extra_headers or [])

    def getheader(self, name, default=None):
        for header, value in self._headers:
            if header.lower() == name.lower():
                return value
        return default

    def getheaders(self):
        return list(self._headers)

    def read(self, amount=None):
        if amount is None:
            amount = len(self._body)
        value = self._body[:amount]
        self._body = self._body[amount:]
        return value

    def read1(self, amount):
        del amount
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeConnection:
    def __init__(
        self,
        factory,
        hostname,
        port,
        connect_timeout,
        response_timeout,
    ):
        self.factory = factory
        self.hostname = hostname
        self.port = port
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self.request_record = None
        self.closed = False

    def request(self, method, target, body=None, headers=None):
        self.request_record = {
            "method": method,
            "target": target,
            "body": body,
            "headers": dict(headers or {}),
        }

    def getresponse(self):
        return self.factory.responses.pop(0)

    def close(self):
        self.closed = True


class FakeConnectionFactory:
    def __init__(self, responses):
        self.responses = list(responses)
        self.connections = []

    def __call__(self, hostname, port, connect_timeout, response_timeout):
        connection = FakeConnection(
            self,
            hostname,
            port,
            connect_timeout,
            response_timeout,
        )
        self.connections.append(connection)
        return connection


def auth_route(*, secret="private-token-123", header_secret="private-key-456"):
    return {
        "name": "docs",
        "url": "https://docs.example.test/mcp",
        "headers": {
            "Authorization": "Bearer %s" % secret,
            "X-API-Key": header_secret,
        },
        "secret_values": (secret, header_secret),
    }


class SkillMcpAuthBrokerTests(unittest.TestCase):
    def test_broker_pins_target_injects_auth_and_redacts_before_client(self):
        secret = "private-token-123"
        header_secret = "private-key-456"
        response = FakeResponse(
            json.dumps(
                {
                    "raw": secret,
                    "escaped": "prefix-%s-suffix" % header_secret,
                }
            ).encode("utf-8"),
            extra_headers=[("Retry-After", secret)],
        )
        factory = FakeConnectionFactory([response])
        with SkillMcpAuthBroker(
            [auth_route(secret=secret, header_secret=header_secret)],
            connection_factory=factory,
        ) as broker:
            parsed = urlsplit(broker.urls["docs"])
            client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            client.request(
                "POST",
                parsed.path,
                body=b'{"jsonrpc":"2.0"}',
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "untrusted-client-value",
                    "X-Ignored": "not-forwarded",
                },
            )
            received = client.getresponse()
            body = received.read()
            client.close()

        self.assertEqual(received.status, 200)
        self.assertNotIn(secret.encode("utf-8"), body)
        self.assertNotIn(header_secret.encode("utf-8"), body)
        self.assertEqual(body.count(b"<redacted>"), 2)
        self.assertEqual(received.getheader("Mcp-Session-Id"), "session-123")
        self.assertEqual(received.getheader("Retry-After"), "<redacted>")
        self.assertIsNone(received.getheader("Set-Cookie"))
        self.assertIsNone(received.getheader("Location"))
        self.assertEqual(received.getheader("Cache-Control"), "no-store")
        connection = factory.connections[0]
        self.assertEqual(connection.hostname, "docs.example.test")
        self.assertEqual(connection.port, 443)
        self.assertEqual(connection.connect_timeout, 10)
        self.assertEqual(connection.response_timeout, 65)
        self.assertEqual(connection.request_record["target"], "/mcp")
        self.assertEqual(
            connection.request_record["headers"]["Authorization"],
            "Bearer %s" % secret,
        )
        self.assertEqual(
            connection.request_record["headers"]["X-API-Key"],
            header_secret,
        )
        self.assertNotIn("X-Ignored", connection.request_record["headers"])
        self.assertTrue(connection.closed)

    def test_stream_redaction_covers_chunk_boundaries(self):
        secret = "private-token-123"
        response = FakeResponse(
            b"",
            content_type="text/event-stream",
            chunks=[b"event: message\ndata: private-to", b"ken-123\n\n"],
        )
        factory = FakeConnectionFactory([response])
        with SkillMcpAuthBroker(
            [auth_route(secret=secret)],
            connection_factory=factory,
        ) as broker:
            parsed = urlsplit(broker.urls["docs"])
            client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            client.request("GET", parsed.path, headers={"Accept": "text/event-stream"})
            received = client.getresponse()
            body = received.read()
            client.close()
        self.assertEqual(received.status, 200)
        self.assertNotIn(secret.encode("utf-8"), body)
        self.assertIn(b"data: <redacted>", body)

    def test_json_escaped_secret_is_redacted_from_body_and_allowed_header(self):
        secret = 'private-"token\\123'
        response = FakeResponse(
            json.dumps({"reflected": secret}).encode("utf-8"),
            extra_headers=[("Mcp-Session-Id", secret)],
        )
        factory = FakeConnectionFactory([response])
        with SkillMcpAuthBroker(
            [auth_route(secret=secret)],
            connection_factory=factory,
        ) as broker:
            parsed = urlsplit(broker.urls["docs"])
            client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            client.request("POST", parsed.path, body=b"{}")
            received = client.getresponse()
            body = received.read()
            client.close()
        self.assertEqual(received.status, 200)
        self.assertEqual(json.loads(body), {"reflected": "<redacted>"})
        session_header = received.getheader("Mcp-Session-Id")
        self.assertIn("<redacted>", session_header)
        self.assertNotIn(secret, session_header)
        self.assertNotIn(secret.encode("utf-8"), body)

    def test_redirect_auth_failure_and_wrong_route_fail_closed(self):
        factory = FakeConnectionFactory(
            [
                FakeResponse(b"redirect", status=302),
                FakeResponse(b"unauthorized", status=401),
            ]
        )
        with SkillMcpAuthBroker(
            [auth_route()],
            connection_factory=factory,
        ) as broker:
            parsed = urlsplit(broker.urls["docs"])
            client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            client.request("POST", parsed.path, body=b"{}")
            redirected = client.getresponse()
            redirected.read()
            client.close()
            wrong = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            wrong.request("POST", "/wrong", body=b"{}")
            missing = wrong.getresponse()
            missing.read()
            wrong.close()
            unauthorized_client = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=2,
            )
            unauthorized_client.request("POST", parsed.path, body=b"{}")
            unauthorized = unauthorized_client.getresponse()
            unauthorized.read()
            unauthorized_client.close()
        self.assertEqual(redirected.status, 502)
        self.assertEqual(missing.status, 404)
        self.assertEqual(unauthorized.status, 502)

    def test_transport_exception_is_generic_and_custom_timeout_is_bounded(self):
        secret = "private-transport-error-123"

        def fail_connection(hostname, port, connect_timeout, response_timeout):
            del hostname, port, connect_timeout
            self.assertEqual(response_timeout, 123)
            raise RuntimeError(secret)

        with SkillMcpAuthBroker(
            [auth_route(secret=secret)],
            connection_factory=fail_connection,
            response_timeout_seconds=123,
        ) as broker:
            parsed = urlsplit(broker.urls["docs"])
            client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            client.request("POST", parsed.path, body=b"{}")
            received = client.getresponse()
            body = received.read()
            client.close()
        self.assertEqual(received.status, 502)
        self.assertNotIn(secret.encode("utf-8"), body)
        with self.assertRaisesRegex(ValidationError, "response timeout"):
            SkillMcpAuthBroker([auth_route()], response_timeout_seconds=0)

    def test_close_forces_a_stalled_local_request_to_release_routes(self):
        factory = FakeConnectionFactory([])
        broker = SkillMcpAuthBroker(
            [auth_route()],
            connection_factory=factory,
        )
        parsed = urlsplit(broker.urls["docs"])
        client = socket.create_connection((parsed.hostname, parsed.port), timeout=2)
        client.sendall(
            (
                "POST %s HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                "Content-Length: 100\r\nConnection: keep-alive\r\n\r\n{"
                % parsed.path
            ).encode("ascii")
        )
        deadline = time.monotonic() + 1
        while not broker._state._client_connections and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(broker._state._client_connections)
        started = time.monotonic()
        broker.close()
        elapsed = time.monotonic() - started
        client.close()
        self.assertLess(elapsed, 2)
        self.assertFalse(broker._thread.is_alive())
        self.assertEqual(broker._state.routes, {})
        self.assertEqual(broker._state._client_connections, set())

    def test_request_response_header_and_method_bounds_fail_closed(self):
        factory = FakeConnectionFactory(
            [
                FakeResponse(b"response-too-large"),
                FakeResponse(b"ok", extra_headers=[("Mcp-Session-Id", "x" * 33)]),
            ]
        )
        with mock.patch(
            "conductor_runtime.skill_mcp_auth_broker.MAX_SKILL_MCP_BROKER_REQUEST_BYTES",
            8,
        ), mock.patch(
            "conductor_runtime.skill_mcp_auth_broker.MAX_SKILL_MCP_BROKER_RESPONSE_BYTES",
            8,
        ), mock.patch(
            "conductor_runtime.skill_mcp_auth_broker.MAX_SKILL_MCP_BROKER_RESPONSE_HEADER_BYTES",
            32,
        ), SkillMcpAuthBroker(
            [auth_route()],
            connection_factory=factory,
        ) as broker:
            parsed = urlsplit(broker.urls["docs"])

            oversized_request_client = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=2,
            )
            oversized_request_client.request("POST", parsed.path, body=b"123456789")
            oversized_request = oversized_request_client.getresponse()
            oversized_request.read()
            oversized_request_client.close()

            oversized_response_client = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=2,
            )
            oversized_response_client.request("POST", parsed.path, body=b"{}")
            oversized_response = oversized_response_client.getresponse()
            oversized_response.read()
            oversized_response_client.close()

            oversized_header_client = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=2,
            )
            oversized_header_client.request("POST", parsed.path, body=b"{}")
            oversized_header = oversized_header_client.getresponse()
            oversized_header.read()
            oversized_header_client.close()

            unsupported_client = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=2,
            )
            unsupported_client.request("PUT", parsed.path, body=b"{}")
            unsupported = unsupported_client.getresponse()
            unsupported_body = unsupported.read()
            unsupported_client.close()

        self.assertEqual(oversized_request.status, 400)
        self.assertEqual(oversized_response.status, 502)
        self.assertEqual(oversized_header.status, 502)
        self.assertEqual(unsupported.status, 405)
        self.assertIn(b"broker rejected", unsupported_body)

    def test_codex_config_override_is_loopback_only_and_omits_auth_fields(self):
        dependencies = [
            {
                "skill": ".agents/skills/docs",
                "name": "docs",
                "url": "https://docs.example.test/mcp",
                "tools": ["search"],
                "auth": {
                    "bearer_token_env_var": "DOCS_TOKEN",
                    "env_http_headers": {"X-API-Key": "DOCS_KEY"},
                },
            }
        ]
        rendered = codex_skill_mcp_config_arg(
            dependencies,
            endpoint_overrides={"docs": "http://127.0.0.1:43123/mcp/random"},
        )
        self.assertIn("http://127.0.0.1:43123/mcp/random", rendered)
        self.assertNotIn("DOCS_TOKEN", rendered)
        self.assertNotIn("DOCS_KEY", rendered)
        self.assertNotIn("bearer_token_env_var", rendered)
        self.assertNotIn("env_http_headers", rendered)
        with self.assertRaisesRegex(ValidationError, "pinned HTTP 127.0.0.1"):
            codex_skill_mcp_config_arg(
                dependencies,
                endpoint_overrides={"docs": "http://localhost:43123/mcp/random"},
            )


if __name__ == "__main__":
    unittest.main()
