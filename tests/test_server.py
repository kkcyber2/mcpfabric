"""The server: protocol handling, and a real round trip over HTTP and stdio."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mcpfabric.openapi import parse
from mcpfabric.server import PROTOCOL_VERSION, Server, serve


SPEC_DOC = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "2"},
    "paths": {
        "/items/{id}": {
            "get": {
                "operationId": "getItem",
                "summary": "Fetch one item",
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
            },
        },
        "/items": {
            "post": {
                "operationId": "createItem",
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {"type": "object"}}}},
            },
        },
    },
}


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_GET(self):  # noqa: N802
        type(self).seen.append(("GET", self.path))
        body = json.dumps({"path": self.path, "name": "widget"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        type(self).seen.append(("POST", self.path, raw))
        if raw.strip() == '{"name": "bad"}':
            body = b'{"error":"rejected"}'
            self.send_response(422)
        else:
            body = b'{"id":7}'
            self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def live():
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def make_server(base="https://api.test", **kw):
    doc = json.loads(json.dumps(SPEC_DOC))
    doc["servers"] = [{"url": base}]
    return Server(parse(doc), **kw)


class TestProtocol:
    def test_initialize_returns_the_version_and_capabilities(self):
        result = make_server().initialize({})
        assert result["protocolVersion"] == PROTOCOL_VERSION
        assert "tools" in result["capabilities"]

    def test_tools_list_covers_every_operation(self):
        names = [t["name"] for t in make_server().tools_list()["tools"]]
        assert names == ["getItem", "createItem"]

    def test_an_unknown_method_is_an_error(self):
        out = make_server().handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})
        assert out["error"]["code"] == -32601

    def test_a_notification_gets_no_response(self):
        out = make_server().handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert out is None

    def test_a_non_2_0_message_is_rejected(self):
        out = make_server().handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        assert out["error"]["code"] == -32600

    def test_ping_answers_with_an_empty_result(self):
        out = make_server().handle({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        assert out == {"jsonrpc": "2.0", "id": 9, "result": {}}

    def test_an_unknown_tool_is_an_error_in_the_content(self):
        out = make_server().tools_call({"name": "ghost", "arguments": {}})
        assert out["isError"] is True
        assert "unknown tool" in out["content"][0]["text"]

    def test_non_object_arguments_are_refused(self):
        out = make_server().tools_call({"name": "getItem", "arguments": []})
        assert out["isError"] is True

    def test_a_missing_required_argument_is_reported_not_sent(self):
        out = make_server().tools_call({"name": "getItem", "arguments": {}})
        assert out["isError"] is True
        assert "missing required" in out["content"][0]["text"]


class TestDryRun:
    def test_dry_run_returns_the_request_and_sends_nothing(self):
        srv = make_server(dry_run=True)
        out = srv.tools_call({"name": "getItem", "arguments": {"id": "3"}})
        text = out["content"][0]["text"]
        assert "dry run" in text
        assert "https://api.test/items/3" in text
        assert out["isError"] is False


class TestLiveHTTP:
    def test_a_get_reaches_the_server_and_returns_the_body(self, live):
        out = make_server(live).tools_call({"name": "getItem", "arguments": {"id": "9"}})
        assert out["isError"] is False
        assert json.loads(out["content"][0]["text"])["path"] == "/items/9"
        assert _Handler.seen[-1] == ("GET", "/items/9")

    def test_a_post_sends_the_body(self, live):
        out = make_server(live).tools_call(
            {"name": "createItem", "arguments": {"body": {"name": "hat"}}})
        assert out["isError"] is False
        method, path, raw = _Handler.seen[-1]
        assert (method, path) == ("POST", "/items")
        assert json.loads(raw) == {"name": "hat"}

    def test_a_422_becomes_an_is_error_result(self, live):
        out = make_server(live).tools_call(
            {"name": "createItem", "arguments": {"body": {"name": "bad"}}})
        assert out["isError"] is True
        assert "422" in out["content"][0]["text"]

    def test_an_unreachable_host_is_an_error_not_a_crash(self):
        srv = make_server("http://127.0.0.1:1", timeout=1.0)
        out = srv.tools_call({"name": "getItem", "arguments": {"id": "1"}})
        assert out["isError"] is True
        assert "failed" in out["content"][0]["text"]


class TestStdioSession:
    def test_a_full_session_over_newline_delimited_json(self, live):
        doc = json.loads(json.dumps(SPEC_DOC))
        doc["servers"] = [{"url": live}]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "getItem", "arguments": {"id": "5"}}},
        ]
        stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
        stdout, stderr = io.StringIO(), io.StringIO()

        assert serve(parse(doc), stdin=stdin, stdout=stdout, stderr=stderr) == 0

        replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
        assert [r["id"] for r in replies] == [1, 2, 3]
        assert len(replies[1]["result"]["tools"]) == 2
        assert json.loads(replies[2]["result"]["content"][0]["text"])["path"] == "/items/5"

    def test_a_malformed_line_yields_a_parse_error_and_the_stream_continues(self):
        stdin = io.StringIO('not json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        stdout = io.StringIO()
        serve(parse(SPEC_DOC), stdin=stdin, stdout=stdout, stderr=io.StringIO())
        replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
        assert replies[0]["error"]["code"] == -32700
        assert replies[1]["result"] == {}

    def test_a_batch_is_answered_as_an_array(self):
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                 {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
        stdin = io.StringIO(json.dumps(batch) + "\n")
        stdout = io.StringIO()
        serve(parse(SPEC_DOC), stdin=stdin, stdout=stdout, stderr=io.StringIO())
        replies = json.loads(stdout.getvalue().strip())
        assert [r["id"] for r in replies] == [1, 2]

    def test_blank_lines_are_ignored(self):
        stdin = io.StringIO("\n\n")
        stdout = io.StringIO()
        serve(parse(SPEC_DOC), stdin=stdin, stdout=stdout, stderr=io.StringIO())
        assert stdout.getvalue() == ""
