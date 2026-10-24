"""Parsing: refs, cycles, and the inputs that must be rejected."""

import pytest

from mcpfabric.openapi import (
    _pointer, _schema_of, _base_url, parse, resolve, SpecError,
)


def doc(**paths):
    return {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": paths,
    }


class TestPointer:
    def test_resolves_a_nested_pointer(self):
        d = {"components": {"schemas": {"Pet": {"type": "object"}}}}
        assert _pointer(d, "#/components/schemas/Pet") == {"type": "object"}

    def test_resolves_an_array_index(self):
        d = {"a": [{"b": 1}, {"b": 2}]}
        assert _pointer(d, "#/a/1") == {"b": 2}

    def test_unescapes_tilde_sequences(self):
        d = {"paths": {"/a~b": {"x": 1}, "/c/d": {"y": 2}}}
        assert _pointer(d, "#/paths/~1a~0b") == {"x": 1}
        assert _pointer(d, "#/paths/~1c~1d") == {"y": 2}

    def test_rejects_remote_references(self):
        with pytest.raises(SpecError, match="only local"):
            _pointer({}, "https://evil.example/schema.json")

    def test_rejects_unknown_pointer(self):
        with pytest.raises(SpecError, match="unresolvable"):
            _pointer({"a": 1}, "#/b")


class TestResolve:
    def test_expands_nested_refs(self):
        d = {"components": {"schemas": {
            "Pet": {"type": "object", "properties": {"tag": {"$ref": "#/components/schemas/Tag"}}},
            "Tag": {"type": "string"},
        }}}
        out = resolve(d, {"$ref": "#/components/schemas/Pet"})
        assert out["properties"]["tag"] == {"type": "string"}

    def test_stops_on_a_self_referencing_cycle(self):
        d = {"components": {"schemas": {
            "Node": {"type": "object",
                     "properties": {"child": {"$ref": "#/components/schemas/Node"}}},
        }}}
        out = resolve(d, {"$ref": "#/components/schemas/Node"})
        # It must terminate and mark the cycle rather than recurse forever.
        assert out["properties"]["child"]["$cycle"] is True

    def test_stops_on_a_mutual_cycle(self):
        d = {"components": {"schemas": {
            "A": {"properties": {"b": {"$ref": "#/components/schemas/B"}}},
            "B": {"properties": {"a": {"$ref": "#/components/schemas/A"}}},
        }}}
        out = resolve(d, {"$ref": "#/components/schemas/A"})
        assert out["properties"]["b"]["properties"]["a"]["$cycle"] is True

    def test_leaves_scalars_untouched(self):
        assert resolve({}, 7) == 7


class TestSchemaOf:
    def test_prefers_an_explicit_schema(self):
        assert _schema_of({"schema": {"type": "integer"}}) == {"type": "integer"}

    def test_reads_swagger_inline_types(self):
        p = {"in": "query", "type": "integer", "format": "int32"}
        assert _schema_of(p) == {"type": "integer", "format": "int32"}

    def test_defaults_to_string_when_nothing_is_declared(self):
        assert _schema_of({"in": "query"}) == {"type": "string"}


class TestBaseUrl:
    def test_prefers_the_servers_block(self):
        assert _base_url(doc()) == "https://api.example.com/v1"

    def test_falls_back_to_swagger_host(self):
        assert _base_url({"host": "h.io", "basePath": "/api", "schemes": ["http"]}) == \
            "http://h.io/api"

    def test_returns_empty_when_undeclared(self):
        assert _base_url({}) == ""


class TestParse:
    def test_reads_a_basic_operation(self):
        spec = parse(doc(**{"/pets": {"get": {"operationId": "listPets"}}}))
        op = spec.operations[0]
        assert (op.method, op.path, op.operation_id) == ("GET", "/pets", "listPets")

    def test_merges_path_and_operation_parameters(self):
        spec = parse(doc(**{"/pets/{id}": {
            "parameters": [{"name": "id", "in": "path", "required": True}],
            "get": {"operationId": "getPet",
                    "parameters": [{"name": "fields", "in": "query"}]},
        }}))
        names = {p.name for p in spec.operations[0].parameters}
        assert names == {"id", "fields"}

    def test_operation_parameter_overrides_the_path_one(self):
        spec = parse(doc(**{"/p": {
            "parameters": [{"name": "q", "in": "query", "required": False}],
            "get": {"parameters": [{"name": "q", "in": "query", "required": True}]},
        }}))
        [param] = spec.operations[0].parameters
        assert param.required is True

    def test_path_parameters_are_always_required(self):
        spec = parse(doc(**{"/p/{id}": {
            "get": {"parameters": [{"name": "id", "in": "path"}]},
        }}))
        assert spec.operations[0].parameters[0].required is True

    def test_reads_a_json_request_body(self):
        spec = parse(doc(**{"/pets": {"post": {
            "requestBody": {"required": True, "content": {
                "application/json": {"schema": {"type": "object"}},
            }},
        }}}))
        op = spec.operations[0]
        assert op.body_schema == {"type": "object"}
        assert op.body_required is True

    def test_reads_a_swagger_body_parameter(self):
        spec = parse(doc(**{"/pets": {"post": {
            "parameters": [{"name": "pet", "in": "body", "required": True,
                            "schema": {"type": "object"}}],
        }}}))
        assert spec.operations[0].body_schema == {"type": "object"}

    def test_accepts_a_swagger_document(self):
        spec = parse({"swagger": "2.0", "host": "h",
                      "paths": {"/x": {"get": {}}}})
        assert spec.operations[0].path == "/x"

    def test_rejects_a_non_object(self):
        with pytest.raises(SpecError, match="root must be an object"):
            parse([])

    def test_rejects_a_document_without_a_version(self):
        with pytest.raises(SpecError, match="openapi"):
            parse({"paths": {"/x": {"get": {}}}})

    def test_rejects_a_document_without_paths(self):
        with pytest.raises(SpecError, match="no paths"):
            parse({"openapi": "3.0.0"})

    def test_rejects_a_document_without_operations(self):
        with pytest.raises(SpecError, match="no operations"):
            parse({"openapi": "3.0.0", "paths": {"/x": {"summary": "nothing"}}})
