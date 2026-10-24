"""Command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .openapi import SpecError, parse
from .server import serve


def _load(path: str) -> dict:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {path} is not valid JSON ({exc})") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcpfabric",
        description="Serve an OpenAPI document as an MCP server.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_cmd = sub.add_parser("serve", help="run the MCP server on stdio")
    serve_cmd.add_argument("spec", help="path to an OpenAPI JSON document, or - for stdin")
    serve_cmd.add_argument("--base-url", help="override the server URL from the spec")
    serve_cmd.add_argument("--timeout", type=float, default=20.0)
    serve_cmd.add_argument("--dry-run", action="store_true",
                           help="answer tool calls with the request that would be sent")

    inspect = sub.add_parser("inspect", help="print the compiled tools")
    inspect.add_argument("spec")
    inspect.add_argument("--base-url")

    args = parser.parse_args(argv)

    try:
        spec = parse(_load(args.spec))
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.base_url:
        spec.base_url = args.base_url

    if args.command == "inspect":
        from .compiler import compile_tools

        payload = [t.as_mcp() for t in compile_tools(spec)]
        print(json.dumps(payload, indent=2))
        return 0

    if not spec.base_url and not args.dry_run:
        print("error: the document declares no server URL; pass --base-url",
              file=sys.stderr)
        return 2

    return serve(spec, timeout=args.timeout, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
