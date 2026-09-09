import os

import pytest
from mcp.client import Client
from mcp.types import ImageContent

from surfsky_mcp import server

pytestmark = pytest.mark.anyio

LIVE = os.environ.get("SURFSKY_LIVE_TESTS") == "1" and os.environ.get("SURFSKY_API_TOKEN")


@pytest.mark.skipif(
    not LIVE, reason="SURFSKY_LIVE_TESTS=1 plus credentials; bills one session"
)
async def test_live_round_trip():
    async with Client(server.mcp, raise_exceptions=True) as client:
        scraped = await client.call_tool(
            "surfsky_scrape",
            {"url": "https://example.com", "formats": ["markdown", "screenshot"]},
        )
        assert (
            not scraped.is_error
            and "Example Domain" in scraped.structured_content["page"]["title"]
        )
        assert isinstance(scraped.content[1], ImageContent)

        started = await client.call_tool("surfsky_session_start", {"idle_timeout": 120})
        session_id = started.structured_content["session_id"]
        try:
            nav = await client.call_tool(
                "surfsky_navigate",
                {"url": "https://example.com", "session_id": session_id},
            )
            assert nav.structured_content["status"] == 200
            refs = [
                row
                for row in nav.structured_content["page"]["snapshot"]
                if row["role"] == "link"
            ]
            assert refs, "example.com has one link"
            acted = await client.call_tool(
                "surfsky_act",
                {
                    "action": "click",
                    "target": f"@{refs[0]['ref']}",
                    "session_id": session_id,
                },
            )
            assert acted.structured_content["navigated"] is True
            read = await client.call_tool(
                "surfsky_read", {"session_id": session_id, "format": "text"}
            )
            assert read.structured_content["page"]["content"]
        finally:
            stopped = await client.call_tool(
                "surfsky_session_stop", {"session_id": session_id}
            )
            assert stopped.structured_content["stopped"] is True
        listed = await client.call_tool("surfsky_session_list", {})
        assert session_id not in {
            row["session_id"] for row in listed.structured_content["sessions"]
        }
