#!/usr/bin/env python3
import json
import sys


PROTOCOL_VERSION = "2025-06-18"
TOOL_NAME = "prove_isolation"


def response(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle(message):
    request_id = message.get("id")
    method = message.get("method")
    if method == "initialize":
        return response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "conductor-local-docs", "version": "1.0.0"},
            },
        )
    if method == "tools/list":
        return response(
            request_id,
            {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": "Return the deterministic local stdio isolation marker.",
                        "inputSchema": {"type": "object", "additionalProperties": False},
                    }
                ]
            },
        )
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Unknown tool"},
            }
        return response(
            request_id,
            {
                "content": [{"type": "text", "text": "SKILL_STDIO_ISOLATION_OK"}],
                "isError": False,
            },
        )
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "Method not found"},
    }


for line in sys.stdin:
    try:
        message = json.loads(line)
        result = handle(message) if isinstance(message, dict) else None
        if result is not None:
            sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    except (BrokenPipeError, OSError):
        break
    except (TypeError, ValueError):
        continue
