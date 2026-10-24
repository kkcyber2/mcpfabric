"""Compile operations into MCP tools, and MCP tool calls into HTTP requests.

Two translations happen here and each has a failure mode worth naming:

* Naming. MCP tool names must match `^[a-zA-Z0-9_-]{1,64}$`. An operation
  without an `operationId`, or with one containing dots or slashes, needs a
  deterministic, collision-free name. Collisions are resolved by suffixing the
  method and a counter, never by silently overwriting.
* Shape. An MCP `inputSchema` is flat-ish JSON Schema (an object of properties).
  Path, query, header and body parameters all have to arrive in one object, so
  each gets a namespaced property and the executor un-namespaces them again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .openapi import Operation, Spec

_NAME_OK = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def tool_name(op: Operation) -> str:
    """A stable, spec-compliant name for an operation."""
    if op.operation_id and _NAME_OK.match(op.operation_id):
        return op.operation_id
    if op.operation_id:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", op.operation_id).strip("_")
        if cleaned:
            # Truncate rather than discard: the id is the author's intent, and
            # any collision it creates is resolved by the compiler.
            return cleaned[:64]
    # Derive from method + path: /pets/{id}/toys -> get_pets_id_toys
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", op.path).strip("_")
    derived = f"{op.method.lower()}_{slug}" if slug else op.method.lower()
    return derived[:64]


def _describe(op: Operation) -> str:
    """Tool descriptions are a prompt, so they must read as instructions."""
    bits: list[str] = []
    if op.summary:
        bits.append(op.summary.strip())
    if op.description:
        first = " ".join(op.description.split())
        if first and first != (op.summary or "").strip():
            bits.append(first[:400])
    if not bits:
        bits.append(f"{op.method} {op.path}")
    bits.append(f"({op.method} {op.path})")
    return " ".join(bits)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    operation: Operation

    def as_mcp(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _prop_name(param) -> str:
    """Query and header parameters can collide by name; keep them distinct."""
    if param.location == "path":
        return param.name
    return f"{param.location}_{param.name}"


def compile_tools(spec: Spec) -> list[Tool]:
    """One tool per operation, with unique names."""
    used: dict[str, int] = {}
    tools: list[Tool] = []

    for op in spec.operations:
        base = tool_name(op)
        if base in used:
            used[base] += 1
            name = f"{base}_{used[base]}"
        else:
            used[base] = 0
            name = base

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in op.parameters:
            key = _prop_name(param)
            prop = dict(param.schema) if param.schema else {"type": "string"}
            prop.setdefault("type", "string")
            prop["description"] = f"{param.location} parameter `{param.name}`"
            properties[key] = prop
            if param.required:
                required.append(key)

        if op.body_schema is not None:
            properties["body"] = {
                "description": f"JSON request body for {op.method} {op.path}",
                **(op.body_schema if isinstance(op.body_schema, dict) else {}),
            }
            # A body schema that is a bare `$ref`-cycle placeholder has no
            # `type`; the executor still accepts an object.
            properties["body"].setdefault("type", "object")
            if op.body_required:
                required.append("body")

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required

        tools.append(Tool(
            name=name,
            description=_describe(op),
            input_schema=schema,
            operation=op,
        ))

    return tools


def build_request(tool: Tool, arguments: dict, base_url: str) -> dict:
    """Turn a tool call into `{method, url, headers, json}`.

    Raises ValueError for a missing required argument or a path placeholder the
    operation never declared - both are caller mistakes that must not be
    papered over with an empty string in the URL.
    """
    op = tool.operation
    args = arguments or {}

    missing = [r for r in tool.input_schema.get("required", []) if r not in args]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")

    path = op.path
    query: dict[str, Any] = {}
    headers: dict[str, str] = {"Accept": "application/json"}
    cookies: dict[str, str] = {}

    for param in op.parameters:
        key = _prop_name(param)
        if key not in args:
            continue
        value = args[key]
        if param.location == "path":
            path = path.replace(f"{{{param.name}}}", _quote(value))
        elif param.location == "query":
            query[param.name] = value
        elif param.location == "header":
            headers[param.name] = str(value)
        elif param.location == "cookie":
            cookies[param.name] = str(value)

    for placeholder in re.findall(r"\{([^}]+)\}", path):
        raise ValueError(f"path parameter `{placeholder}` was not supplied")

    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{_encode(query)}"

    request: dict[str, Any] = {"method": op.method, "url": url, "headers": headers}
    if "body" in args:
        request["json"] = args["body"]
        headers["Content-Type"] = "application/json"
    return request


def _quote(value: Any) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


def _encode(query: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    pairs: list[tuple[str, str]] = []
    for key, value in query.items():
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(v)) for v in value)
        elif isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        else:
            pairs.append((key, str(value)))
    return urlencode(pairs)
