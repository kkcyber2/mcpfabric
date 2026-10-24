"""Compile an OpenAPI document into a working MCP server."""

from .compiler import Tool, build_request, compile_tools, tool_name
from .openapi import Operation, Parameter, Spec, SpecError, parse
from .server import Server, serve

__all__ = [
    "Operation",
    "Parameter",
    "Server",
    "Spec",
    "SpecError",
    "Tool",
    "build_request",
    "compile_tools",
    "parse",
    "serve",
    "tool_name",
]

__version__ = "1.0.0"
