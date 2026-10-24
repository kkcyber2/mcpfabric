"""A real MCP server over stdio, backed by a compiled OpenAPI operation set.

The transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, which is what
MCP stdio clients speak. Logs go to stderr; writing them to stdout would
corrupt the stream.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, TextIO

from .compiler import Tool, build_request, compile_tools
from .openapi import Spec

PROTOCOL_VERSION = "2024-11-05"


class Server:
    """Handles MCP methods against a fixed tool set."""

    def __init__(self, spec: Spec, *, timeout: float = 20.0,
                 dry_run: bool = False) -> None:
        self.spec = spec
        self.tools: list[Tool] = compile_tools(spec)
        self.by_name = {t.name: t for t in self.tools}
        self.timeout = timeout
        self.dry_run = dry_run

    # -- MCP methods -----------------------------------------------------
    def initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mcpfabric", "version": "1.0.0"},
        }

    def tools_list(self) -> dict:
        return {"tools": [t.as_mcp() for t in self.tools]}

    def tools_call(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or name not in self.by_name:
            return _error_content(f"unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error_content("`arguments` must be an object")

        tool = self.by_name[name]
        try:
            request = build_request(tool, arguments, self.spec.base_url)
        except ValueError as exc:
            return _error_content(str(exc))

        if self.dry_run:
            return _text_content(
                "dry run: request not sent\n" + json.dumps(request, indent=2)
            )

        try:
            status, body = _fetch(request, self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:4000]
            return _error_content(f"HTTP {exc.code} from {request['url']}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return _error_content(f"request to {request['url']} failed: {exc}")

        if status >= 400:
            return _error_content(f"HTTP {status} from {request['url']}: {body[:4000]}")
        return _text_content(body or f"HTTP {status} with an empty body")

    # -- dispatch --------------------------------------------------------
    def handle(self, message: dict) -> dict | None:
        """Return a response object, or None for a notification."""
        is_notification = "id" not in message
        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return None if is_notification else _rpc_error(
                msg_id, -32600, "invalid JSON-RPC 2.0 request"
            )

        try:
            if method == "initialize":
                result = self.initialize(params)
            elif method == "tools/list":
                result = self.tools_list()
            elif method == "tools/call":
                result = self.tools_call(params)
            elif method == "ping":
                result = {}
            elif method in ("notifications/initialized", "notifications/cancelled"):
                return None
            else:
                return None if is_notification else _rpc_error(
                    msg_id, -32601, f"method {method!r} is not supported"
                )
        except Exception as exc:  # noqa: BLE001 - a bad call must not kill the server
            return None if is_notification else _rpc_error(msg_id, -32603, str(exc))

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _fetch(request: dict, timeout: float) -> tuple[int, str]:
    data = None
    if "json" in request:
        data = json.dumps(request["json"]).encode("utf-8")
    req = urllib.request.Request(
        request["url"], data=data, headers=request.get("headers", {}),
        method=request["method"],
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", "replace")


def _text_content(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error_content(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _rpc_error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def serve(spec: Spec, stdin: TextIO = None, stdout: TextIO = None,
          stderr: TextIO = None, **kwargs: Any) -> int:
    """Read newline-delimited JSON-RPC until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    server = Server(spec, **kwargs)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_rpc_error(None, -32700, "parse error")),
                  file=stdout, flush=True)
            continue

        if isinstance(message, list):
            responses = [r for r in (server.handle(m) for m in message if isinstance(m, dict)) if r]
            if responses:
                print(json.dumps(responses), file=stdout, flush=True)
            continue

        if not isinstance(message, dict):
            print(json.dumps(_rpc_error(None, -32600, "expected an object")),
                  file=stdout, flush=True)
            continue

        response = server.handle(message)
        if response is not None:
            print(json.dumps(response), file=stdout, flush=True)

    print("mcpfabric: stdin closed", file=stderr)
    return 0
