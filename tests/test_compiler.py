"""Naming, schema shape, and request building."""

import pytest

from mcpfabric.compiler import build_request, compile_tools, tool_name
from mcpfabric.openapi import Operation, Parameter, parse


def spec_of(paths):
    return parse({"openapi": "3.0.3", "info": {"title": "T", "version": "1"},
                  "servers": [{"url": "https://api.test"}], "paths": paths})


def op(**kw):
    base = dict(method="GET", path="/x", operation_id="", summary="", description="",
                parameters=(), body_schema=None, body_required=False, deprecated=False)
    base.update(kw)
    return Operation(**base)


class TestToolName:
    def test_keeps_a_valid_operation_id(self):
        assert tool_name(op(operation_id="list_pets")) == "list_pets"

    def test_sanitises_dots_and_slashes(self):
        assert tool_name(op(operation_id="pets.list/all")) == "pets_list_all"

    def test_derives_from_the_path_when_no_id(self):
        assert tool_name(op(method="GET", path="/pets/{id}/toys")) == "get_pets_id_toys"

    def test_handles_a_root_path(self):
        assert tool_name(op(method="GET", path="/")) == "get"

    def test_truncates_to_64_characters(self):
        assert len(tool_name(op(operation_id="a" * 200))) == 64

    def test_a_non_ascii_id_falls_back_to_the_path(self):
        name = tool_name(op(operation_id="日本語", path="/pets"))
        assert name == "get_pets"


class TestCompile:
    def test_one_tool_per_operation(self):
        tools = compile_tools(spec_of({
            "/a": {"get": {"operationId": "a"}},
            "/b": {"post": {"operationId": "b"}},
        }))
        assert [t.name for t in tools] == ["a", "b"]

    def test_colliding_names_are_suffixed_not_overwritten(self):
        tools = compile_tools(spec_of({
            "/a": {"get": {"operationId": "same"}},
            "/b": {"get": {"operationId": "same"}},
            "/c": {"get": {"operationId": "same"}},
        }))
        assert [t.name for t in tools] == ["same", "same_1", "same_2"]

    def test_path_and_query_of_the_same_name_stay_distinct(self):
        tools = compile_tools(spec_of({"/p/{id}": {"get": {
            "parameters": [
                {"name": "id", "in": "path"},
                {"name": "id", "in": "query"},
            ],
        }}}))
        props = tools[0].input_schema["properties"]
        assert set(props) == {"id", "query_id"}

    def test_required_parameters_are_marked_required(self):
        tools = compile_tools(spec_of({"/p/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path"},
                           {"name": "q", "in": "query"}],
        }}}))
        assert tools[0].input_schema["required"] == ["id"]

    def test_a_required_body_is_marked_required(self):
        tools = compile_tools(spec_of({"/p": {"post": {
            "requestBody": {"required": True, "content": {
                "application/json": {"schema": {"type": "object"}}}},
        }}}))
        assert "body" in tools[0].input_schema["required"]

    def test_the_schema_rejects_undeclared_arguments(self):
        tools = compile_tools(spec_of({"/p": {"get": {"operationId": "p"}}}))
        assert tools[0].input_schema["additionalProperties"] is False

    def test_the_description_names_the_route(self):
        tools = compile_tools(spec_of({"/pets": {"get": {
            "operationId": "listPets", "summary": "List pets"}}}))
        assert "List pets" in tools[0].description
        assert "GET /pets" in tools[0].description

    def test_a_body_without_declared_properties_still_types_as_object(self):
        tools = compile_tools(spec_of({"/p": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {}}}}}}}))
        assert tools[0].input_schema["properties"]["body"]["type"] == "object"

    def test_as_mcp_uses_the_protocol_field_names(self):
        tools = compile_tools(spec_of({"/p": {"get": {"operationId": "p"}}}))
        payload = tools[0].as_mcp()
        assert set(payload) == {"name", "description", "inputSchema"}


class TestBuildRequest:
    def _tool(self, **kw):
        return compile_tools(spec_of(kw))[0]

    def test_substitutes_a_path_parameter(self):
        tool = self._tool(**{"/p/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path"}]}}})
        req = build_request(tool, {"id": "42"}, "https://api.test")
        assert req["url"] == "https://api.test/p/42"

    def test_url_encodes_a_path_parameter(self):
        tool = self._tool(**{"/p/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path"}]}}})
        req = build_request(tool, {"id": "../admin"}, "https://api.test")
        assert req["url"] == "https://api.test/p/..%2Fadmin"

    def test_appends_query_parameters(self):
        tool = self._tool(**{"/p": {"get": {
            "parameters": [{"name": "limit", "in": "query"}]}}})
        req = build_request(tool, {"query_limit": 5}, "https://api.test")
        assert req["url"] == "https://api.test/p?limit=5"

    def test_encodes_a_list_query_as_repeated_keys(self):
        tool = self._tool(**{"/p": {"get": {
            "parameters": [{"name": "tag", "in": "query"}]}}})
        req = build_request(tool, {"query_tag": ["a", "b"]}, "https://api.test")
        assert req["url"].endswith("tag=a&tag=b")

    def test_renders_a_boolean_query_lowercase(self):
        tool = self._tool(**{"/p": {"get": {
            "parameters": [{"name": "on", "in": "query"}]}}})
        req = build_request(tool, {"query_on": True}, "https://api.test")
        assert req["url"].endswith("on=true")

    def test_moves_a_header_parameter_into_headers(self):
        tool = self._tool(**{"/p": {"get": {
            "parameters": [{"name": "X-Trace", "in": "header"}]}}})
        req = build_request(tool, {"header_X-Trace": "abc"}, "https://api.test")
        assert req["headers"]["X-Trace"] == "abc"
        assert "X-Trace" not in req["url"]

    def test_folds_cookies_into_one_cookie_header(self):
        tool = self._tool(**{"/p": {"get": {
            "parameters": [{"name": "a", "in": "cookie"},
                           {"name": "b", "in": "cookie"}]}}})
        req = build_request(tool, {"cookie_a": "1", "cookie_b": "2"}, "https://api.test")
        assert req["headers"]["Cookie"] == "a=1; b=2"

    def test_sends_a_body_as_json(self):
        tool = self._tool(**{"/p": {"post": {
            "requestBody": {"content": {"application/json": {
                "schema": {"type": "object"}}}}}}})
        req = build_request(tool, {"body": {"name": "x"}}, "https://api.test")
        assert req["json"] == {"name": "x"}
        assert req["headers"]["Content-Type"] == "application/json"

    def test_rejects_a_missing_required_argument(self):
        tool = self._tool(**{"/p/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path"}]}}})
        with pytest.raises(ValueError, match="missing required"):
            build_request(tool, {}, "https://api.test")

    def test_rejects_an_undeclared_path_placeholder(self):
        # The document says `{id}` in the path but never declares the parameter,
        # so the placeholder can never be filled. That must fail loudly.
        tool = self._tool(**{"/p/{id}": {"get": {"operationId": "p"}}})
        with pytest.raises(ValueError, match="not supplied"):
            build_request(tool, {}, "https://api.test")

    def test_a_trailing_slash_in_the_base_url_does_not_double_up(self):
        tool = self._tool(**{"/p": {"get": {"operationId": "p"}}})
        assert build_request(tool, {}, "https://api.test/")["url"] == "https://api.test/p"
