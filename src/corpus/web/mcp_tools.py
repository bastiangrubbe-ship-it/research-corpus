"""MCP tool catalog and ad-hoc tester.

Deliberately *not* a process monitor. The MCP server is a stdio subprocess spawned
per client, not a long-running daemon, so there is no "is it up" to report. What is
worth showing is the contract — which tools exist, what arguments they take, what
they return — and a way to exercise one without leaving the browser.

Calls go through the same `corpus.mcp.server` singleton an MCP client would reach,
so what this panel returns is what Claude Code sees for the same arguments. The
server resolves its tenant once at import from configuration; nothing routed through
here can change that, which is why exposing a call endpoint on a localhost-only
dashboard is not a new authorization surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from corpus.mcp.server import server

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


@router.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    """Registered tools with their JSON-Schema argument contracts."""
    tools = await server.list_tools()
    out: list[dict[str, Any]] = []
    for tool in tools:
        data = tool.model_dump()
        schema = data.get("input_schema") or {}
        out.append(
            {
                "name": data.get("name"),
                "description": (data.get("description") or "").strip(),
                "properties": schema.get("properties") or {},
                "required": schema.get("required") or [],
            }
        )
    return out


class CallToolRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


@router.post("/call")
async def call_tool(body: CallToolRequest) -> dict[str, Any]:
    """Invoke one tool and return what an MCP client would receive.

    `server.call_tool` raises `ToolError` rather than returning `is_error=True` —
    that conversion happens at the protocol layer, which this path bypasses. Caught
    here and turned into a 400 so a bad argument reads as a user error, not a crash.
    """
    try:
        result = await server.call_tool(body.name, body.arguments)
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # `content` is always populated; `structured_content` is not (it depends on the
    # tool's return shape), so the text blocks are the reliable thing to render.
    text_blocks = [getattr(block, "text", "") for block in (result.content or [])]
    return {
        "name": body.name,
        "is_error": bool(result.is_error),
        "text": "\n".join(t for t in text_blocks if t),
        "structured": result.structured_content
        if isinstance(result.structured_content, dict)
        else None,
    }
