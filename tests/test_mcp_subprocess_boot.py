"""MCP subprocess boot test — proves all three servers spawn cleanly.

Without this test, a syntax error or import failure in any of the three MCP
server scripts would only surface at first real run (when the user has
provisioned credentials). This test catches it earlier.

Caveat: the email and slack servers may require env vars to fully initialize
clients — but module-level startup should not. We verify the subprocess can
be SPAWNED via the MCP stdio handshake; we do NOT call any tools.
"""

from __future__ import annotations

import sys
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import src.mcp_client as mcp_client
from src.mcp_client import MCPClientRouter


@pytest.mark.asyncio
async def test_mcp_router_spawns_all_three_servers() -> None:
    """`async with MCPClientRouter()` must complete without raising.

    This proves:
      - `support_read.py` boots, registers tools, completes the MCP handshake
      - `support_email_write.py` boots ditto (no SMTP connection attempted)
      - `support_slack_write.py` boots ditto (no Slack API call attempted)
      - `mcp_client.MCPClientRouter` correctly enters all three async contexts

    Real tool calls are tested via the integration smoke (with mocked routers)
    and at demo time (with real credentials).
    """
    async with MCPClientRouter() as router:
        # All three sub-clients must be live after __aenter__.
        assert router.read is not None, "Read MCP server failed to spawn"
        assert router.email is not None, "Email MCP server failed to spawn"
        assert router.slack is not None, "Slack MCP server failed to spawn"


@pytest.mark.asyncio
async def test_mcp_subprocess_uses_repository_root_as_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP children must resolve imports from the repository package root."""
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_stdio(server_params: object, **_kwargs: object):
        captured["server_params"] = server_params
        yield (object(), object())

    class FakeSession:
        def __init__(self, *_streams: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)

    async with mcp_client._make_session(sys.executable, str(mcp_client._EMAIL_SERVER)):
        pass

    server_params = captured["server_params"]
    assert Path(server_params.cwd).resolve() == Path(mcp_client.__file__).resolve().parent.parent
    python_path = (server_params.env or {}).get("PYTHONPATH", "")
    assert str(Path(mcp_client.__file__).resolve().parent.parent) in python_path.split(
        os.pathsep
    )
