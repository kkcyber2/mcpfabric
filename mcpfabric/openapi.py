"""OpenAPI reading: reference resolution, operations, and their parameters.

Refs are resolved *lazily and boundedly*. An OpenAPI document may contain a
cycle (`Node.properties.child -> Node`), and a naive deep resolver will spin
forever on it. Resolution therefore tracks the reference path it is currently
expanding and stops when a reference revisits itself, leaving a placeholder
behind rather than inventing a shape.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class SpecError(ValueError):
    """The document is not usable as an OpenAPI description."""


_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def _pointer(doc: dict, ref: str) -> Any:
    """Resolve a local JSON pointer such as `#/components/schemas/Pet`."""
    if not ref.startswith("#/"):
        raise SpecError(f"only local references are supported, got {ref!r}")
    node: Any = doc
    for raw in ref[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise SpecError(f"unresolvable reference {ref!r}")
    return node


def resolve(doc: dict, node: Any, _seen: tuple[str, ...] = ()) -> Any:
    """Resolve every `$ref` under `node`, stopping on cycles."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in _seen:
                # A cycle. Keep the pointer so callers can see what it was.
                return {"$ref": ref, "$cycle": True}
            target = _pointer(doc, ref)
            return resolve(doc, copy.deepcopy(target), _seen + (ref,))
        return {k: resolve(doc, v, _seen) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(doc, item, _seen) for item in node]
    return node


@dataclass(frozen=True)
class Parameter:
    name: str
    location: str          # path | query | header | cookie
    required: bool
    schema: dict

    @property
    def is_body(self) -> bool:
        return self.location == "body"


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    operation_id: str
    summary: str
    description: str
    parameters: tuple[Parameter, ...]
    body_schema: dict | None
    body_required: bool
    deprecated: bool
    tags: tuple[str, ...] = ()


@dataclass
class Spec:
    title: str
    version: str
    base_url: str
    operations: list[Operation] = field(default_factory=list)


def _schema_of(param: dict) -> dict:
    """OpenAPI 3 keeps `schema`; 2.0 inlines the type onto the parameter."""
    if isinstance(param.get("schema"), dict):
        return dict(param["schema"])
    inline = {
        k: v for k, v in param.items()
        if k in ("type", "format", "enum", "default", "minimum", "maximum",
                 "minLength", "maxLength", "pattern", "items")
    }
    return inline or {"type": "string"}


def _base_url(doc: dict) -> str:
    servers = doc.get("servers")
    if isinstance(servers, list) and servers:
        url = servers[0].get("url")
        if isinstance(url, str) and url:
            return url.rstrip("/")
    host = doc.get("host")
    if isinstance(host, str) and host:
        scheme = (doc.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{doc.get('basePath', '')}".rstrip("/")
    return ""


def parse(doc: dict) -> Spec:
    """Turn a document into operations. Raises SpecError when unusable."""
    if not isinstance(doc, dict):
        raise SpecError("the document root must be an object")
    if "openapi" not in doc and "swagger" not in doc:
        raise SpecError("missing `openapi` or `swagger` version field")
    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise SpecError("the document declares no paths")

    spec = Spec(
        title=str(doc.get("info", {}).get("title", "untitled")),
        version=str(doc.get("info", {}).get("version", "")),
        base_url=_base_url(doc),
    )

    for raw_path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in _HTTP_METHODS:
            raw = item.get(method)
            if not isinstance(raw, dict):
                continue

            merged: dict[str, dict] = {}
            for param in list(shared) + list(raw.get("parameters") or []):
                resolved = resolve(doc, param)
                if isinstance(resolved, dict) and "name" in resolved:
                    # Operation-level parameters override path-level ones.
                    merged[f"{resolved.get('in')}:{resolved['name']}"] = resolved

            params = tuple(
                Parameter(
                    name=p["name"],
                    location=str(p.get("in", "query")),
                    required=bool(p.get("required", False)) or p.get("in") == "path",
                    schema=_schema_of(p),
                )
                for p in merged.values()
                if p.get("in") in ("path", "query", "header", "cookie")
            )

            body_schema = None
            body_required = False
            if isinstance(raw.get("requestBody"), dict):
                resolved_body = resolve(doc, raw["requestBody"])
                body_required = bool(resolved_body.get("required", False))
                content = resolved_body.get("content") or {}
                for media in ("application/json", "application/*+json"):
                    if media in content:
                        body_schema = content[media].get("schema") or {}
                        break
                else:
                    if content:
                        first = next(iter(content.values()))
                        body_schema = first.get("schema") or {}
            elif isinstance(raw.get("parameters"), list):
                for p in raw["parameters"]:
                    if isinstance(p, dict) and p.get("in") == "body":
                        body_schema = resolve(doc, p.get("schema") or {})
                        body_required = bool(p.get("required", False))
                        break

            spec.operations.append(Operation(
                method=method.upper(),
                path=raw_path,
                operation_id=str(raw.get("operationId") or ""),
                summary=str(raw.get("summary") or ""),
                description=str(raw.get("description") or ""),
                parameters=params,
                body_schema=body_schema,
                body_required=body_required,
                deprecated=bool(raw.get("deprecated", False)),
                tags=tuple(raw.get("tags") or ()),
            ))

    if not spec.operations:
        raise SpecError("the document declares no operations")
    return spec
