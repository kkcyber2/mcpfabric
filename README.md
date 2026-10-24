# mcpfabric

Turn an OpenAPI document into a working MCP server.

Most APIs already describe themselves in OpenAPI. MCP clients — Claude Desktop,
and the growing set of agent runtimes that speak the protocol — want tools. The
gap between the two is mechanical: names have to be legal, parameters have to be
merged into one input schema, path placeholders have to be filled and encoded,
and errors have to come back as tool results rather than killing the process.
This does that, with no dependencies.

```bash
pip install -e ".[dev]"
mcpfabric inspect api.json          # see the tools before wiring anything up
mcpfabric serve api.json            # run the MCP server on stdio
```

## Wiring it into a client

Add to the client's MCP configuration:

```json
{
  "mcpServers": {
    "shop": {
      "command": "python3",
      "args": ["-m", "mcpfabric", "serve", "/absolute/path/to/openapi.json"]
    }
  }
}
```

If the document has no `servers` block, pass `--base-url https://api.example.com`.
To check the request shapes without touching the network, add `--dry-run`: every
tool call returns the request that *would* have been sent.

## What it handles, and why each part matters

**Names.** MCP requires `^[a-zA-Z0-9_-]{1,64}$`. Operation ids containing dots or
slashes are sanitised; a missing id falls back to `method_path_slug`
(`GET /pets/{id}/toys` → `get_pets_id_toys`). Two operations that sanitise to the
same name get `_1`, `_2` suffixes — the compiler counts collisions rather than
overwriting a tool.

**Input schemas.** Path, query, header, and cookie parameters all have to arrive
in one object. Path and query parameters keep their own names; query and header
parameters are namespaced (`query_limit`, `header_X-Trace`) so a path parameter
`id` and a query parameter `id` can coexist. Path parameters are always required,
whatever the document claims, because the URL cannot be built without them.

**References.** Local `$ref`s are expanded. Cycles are *not* followed forever: a
schema that refers to itself (`Node.child -> Node`) resolves to a marked
placeholder instead of hanging or inventing a depth limit. Remote `$ref`s are
rejected rather than fetched — no network call is made while parsing a document
you were handed.

**Requests.** Path values are percent-encoded, so a caller cannot traverse out of
the documented route with `../`. List query values become repeated keys; booleans
render lowercase. A missing required argument, or a path placeholder the document
never declared, raises instead of sending a malformed URL.

**Failures.** An HTTP 4xx/5xx, an unreachable host, or a malformed call comes back
as an `isError: true` tool result. The server keeps running; one bad call does not
take down a session.

## Protocol

Newline-delimited JSON-RPC 2.0 over stdin/stdout, protocol version `2024-11-05`.
Supports `initialize`, `tools/list`, `tools/call`, `ping`, the `notifications/*`
messages, and batches. Malformed JSON produces a `-32700` parse error and the
stream continues. Logs go to stderr so they cannot corrupt the protocol stream.

## Tests

```bash
python3 -m pytest -v
```

70 tests. The suite includes a real `http.server` fixture and a full
`initialize → tools/list → tools/call` session driven through `serve()`, so the
transport is exercised rather than mocked.

## Limits worth knowing

- Local `$ref` only. Documents that split schemas across files need to be bundled first.
- `application/json` request bodies only; multipart and form encodings are not built.
- Response schemas are not validated — whatever the API returns is passed through.
- No authentication handling. Put a token into an argument, or front the API with
  something that adds credentials.

## Licence

MIT. Copyright (c) 2026 Konain Sultan Khan.
