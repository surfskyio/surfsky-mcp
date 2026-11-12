import hashlib
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from surfsky import AuthenticationError, Region, ServerError

from surfsky_mcp import server
from surfsky_mcp.sessions import Hosted
from tests.conftest import FakeClient, error_of, server_error, text_of

pytestmark = pytest.mark.anyio

URL = "http://mcp.test/mcp"
REGIONS = {
    "tok-de": {"cluster": "de2", "url": ""},
    "tok-us": {"cluster": "us1", "url": ""},
    "tok-own": {"cluster": "de2", "url": "https://api-own.test"},
    "tok-new": {"cluster": None, "url": ""},
}


class ApiClient(FakeClient):
    def __init__(self, api_token, base_url, lookups):
        super().__init__()
        self.token, self.base_url, self.lookups = api_token, base_url, lookups
        self.account = SimpleNamespace(region=self.region)

    async def region(self):
        self.lookups.append(self.token)
        if self.token == "tok-5xx":
            raise server_error()
        if self.token not in REGIONS:
            raise AuthenticationError("bad token", status_code=401)
        return Region(**REGIONS[self.token])


@pytest.fixture
async def hosted(fakes):
    made, lookups = [], []

    def make_client(api_token=None, base_url=None):
        made.append(ApiClient(api_token, base_url, lookups))
        return made[-1]

    accounts = Hosted(
        make_client,
        "https://api-de2.test",
        {"de2": "https://api-de2.test", "us1": "https://api-us1.test"},
    )
    app = server.hosted_app(accounts, ["mcp.test"])
    async with app.router.lifespan_context(app):
        yield SimpleNamespace(app=app, made=made, lookups=lookups, accounts=accounts)


def http(app, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://mcp.test",
        headers=headers,
    )


@asynccontextmanager
async def connect(hosted, token, mode="legacy"):
    async with (
        http(hosted.app, token) as client,
        Client(streamable_http_client(URL, http_client=client), mode=mode) as mcp_client,
    ):
        yield mcp_client


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "x", "version": "0"},
    },
}
ACCEPT = {"Accept": "application/json, text/event-stream"}


async def test_no_token_and_unknown_token_get_401(hosted):
    async with http(hosted.app) as anonymous:
        assert (
            await anonymous.post("/mcp", json=INIT, headers=ACCEPT)
        ).status_code == 401
    async with http(hosted.app, "tok-bad") as stranger:
        for _ in range(2):
            reply = await stranger.post("/mcp", json=INIT, headers=ACCEPT)
            assert reply.status_code == 401
    assert hosted.lookups == ["tok-bad"]  # a refused token is not looked up again


async def test_token_cache_drops_the_least_recently_used(hosted, monkeypatch):
    from surfsky_mcp import sessions

    monkeypatch.setattr(sessions, "MAX_VERIFIED", 2)
    for token in ("tok-de", "tok-us", "tok-de", "tok-own", "tok-de", "tok-us"):
        await hosted.accounts.verify_token(token)
    # tok-own pushed out tok-us, the one not used since; tok-de stayed throughout
    assert hosted.lookups == ["tok-de", "tok-us", "tok-own", "tok-us"]


async def test_failed_lookup_is_an_error_and_is_not_remembered(hosted):
    async with http(hosted.app, "tok-5xx") as client:
        for _ in range(2):
            with pytest.raises(ServerError):
                await client.post("/mcp", json=INIT, headers=ACCEPT)
    assert hosted.lookups == ["tok-5xx", "tok-5xx"]


@pytest.mark.parametrize(
    "token, base_url",
    [
        ("tok-de", "https://api-de2.test"),
        ("tok-us", "https://api-us1.test"),
        ("tok-own", "https://api-own.test"),
    ],
)
async def test_scrape_goes_to_the_accounts_api_host(hosted, token, base_url):
    async with connect(hosted, token) as client:
        result = await client.call_tool("surfsky_scrape", {"url": "e.test"})
    assert "# Hi" in text_of(result)
    used = [c for c in hosted.made if c.scrapes]
    assert [(c.token, c.base_url) for c in used] == [(token, base_url)]
    assert hosted.lookups == [token]  # one lookup, against the configured host
    assert hosted.made[0].base_url == "https://api-de2.test"


async def test_account_without_a_region_is_pointed_to_the_dashboard(hosted):
    async with connect(hosted, "tok-new") as client:
        error = error_of(await client.call_tool("surfsky_scrape", {"url": "e.test"}))
    assert "region" in error["message"] and "app.surfsky.io" in error["message"]
    assert not any(c.scrapes for c in hosted.made)


async def test_account_on_an_unserved_cluster_is_told_so(hosted, monkeypatch):
    monkeypatch.setitem(REGIONS, "tok-de1", {"cluster": "de1", "url": ""})
    async with connect(hosted, "tok-de1") as client:
        error = error_of(await client.call_tool("surfsky_scrape", {"url": "e.test"}))
    assert "de1" in error["message"]


async def test_status_reports_the_callers_account(hosted, monkeypatch):
    from surfsky_mcp import tools

    seen = {}

    async def cloud_status(settings):
        seen.update(token=settings.token()[0], url=settings.url())
        return {"plan": "pro"}

    monkeypatch.setattr(tools, "cloud_status", cloud_status)
    async with connect(hosted, "tok-us") as client:
        data = (await client.call_tool("surfsky_status", {})).structured_content
    assert seen == {"token": "tok-us", "url": "https://api-us1.test"}
    assert data["base_url"] == "https://api-us1.test" and data["plan"] == "pro"


async def test_started_session_is_the_connections_current_session(hosted):
    async with connect(hosted, "tok-de") as client:
        no_session = error_of(
            await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        )
        assert no_session["code"] == "no_session"
        assert "surfsky_session_start" in no_session["message"]
        assert not any(c.profiles.started for c in hosted.made)
        started = (await client.call_tool("surfsky_session_start", {})).structured_content
        moved = (
            await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        ).structured_content
        assert moved["session_id"] == started["session_id"]
        second = (await client.call_tool("surfsky_session_start", {})).structured_content
        named = (
            await client.call_tool(
                "surfsky_navigate",
                {"url": "https://e.test/a", "session_id": started["session_id"]},
            )
        ).structured_content
        current = (
            await client.call_tool("surfsky_navigate", {"url": "https://e.test/b"})
        ).structured_content
    assert named["session_id"] == started["session_id"]  # an explicit id wins
    assert current["session_id"] == second["session_id"]


async def test_two_connections_on_one_token_keep_their_own_session(hosted):
    async with connect(hosted, "tok-de") as one, connect(hosted, "tok-de") as two:
        first = (await one.call_tool("surfsky_session_start", {})).structured_content
        second = (await two.call_tool("surfsky_session_start", {})).structured_content
        on_one = (
            await one.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        ).structured_content
        on_two = (
            await two.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        ).structured_content
    assert first["session_id"] != second["session_id"]
    assert on_one["session_id"] == first["session_id"]
    assert on_two["session_id"] == second["session_id"]


async def test_another_token_cannot_use_or_stop_a_session(hosted):
    async with connect(hosted, "tok-de") as owner, connect(hosted, "tok-us") as other:
        mine = (await owner.call_tool("surfsky_session_start", {})).structured_content
        sid = mine["session_id"]
        denied = error_of(
            await other.call_tool(
                "surfsky_navigate", {"url": "https://e.test/", "session_id": sid}
            )
        )
        assert denied["code"] == "no_session"
        await other.call_tool("surfsky_session_stop", {"session_id": sid})
        listed = (await other.call_tool("surfsky_session_list", {})).structured_content
        assert all(not row["owned"] for row in listed["sessions"])
        still = (
            await owner.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        ).structured_content
    assert still["session_id"] == sid
    owner_api = next(c for c in hosted.made if c.token == "tok-de" and c.profiles.started)
    assert owner_api.profiles.stopped == []


async def test_session_outlives_its_connection_and_is_stopped_on_shutdown(hosted):
    async with connect(hosted, "tok-de") as client:
        sid = (await client.call_tool("surfsky_session_start", {})).structured_content[
            "session_id"
        ]
    api = next(c for c in hosted.made if c.profiles.started)
    assert api.profiles.stopped == []
    async with connect(hosted, "tok-de") as again:
        moved = await again.call_tool(
            "surfsky_navigate", {"url": "https://e.test/", "session_id": sid}
        )
        assert moved.structured_content["session_id"] == sid
    await hosted.accounts.stop_all()
    assert api.profiles.stopped == [sid]


async def test_idle_session_is_reaped(hosted):
    async with connect(hosted, "tok-de") as client:
        started = await client.call_tool("surfsky_session_start", {"idle_timeout": 60})
        sid = started.structured_content["session_id"]
        registry = next(iter(hosted.accounts.registries.values()))
        live = registry.sessions[sid]
        assert await registry.reap_once(now=live.last_used + 61) == [sid]
        gone = error_of(
            await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        )
    assert gone["code"] == "no_session" and "surfsky_session_start" in gone["message"]


async def test_nothing_is_written_to_disk_and_tokens_stay_out_of_logs(
    hosted, env, caplog
):
    caplog.set_level(logging.DEBUG)
    async with connect(hosted, "tok-de") as client:
        sid = (await client.call_tool("surfsky_session_start", {})).structured_content[
            "session_id"
        ]
        await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        api = next(c for c in hosted.made if c.profiles.started)
        api.profiles.fail_stop = server_error()
        await client.call_tool("surfsky_session_stop", {"session_id": sid})
        await hosted.accounts.stop_all()
    assert list(env.iterdir()) == []
    digest = hashlib.sha256(b"tok-de").hexdigest()[:12]
    assert "tok-de" not in caplog.text and digest in caplog.text


async def test_hosted_instructions_say_to_start_a_session_first(hosted):
    async with connect(hosted, "tok-de") as client:
        text = client.instructions
    assert "surfsky_session_start" in text and "default session" not in text
    assert len(text.encode()) <= 2048
    assert "default session" in server.INSTRUCTIONS


async def test_without_a_stable_connection_the_agent_passes_session_id(hosted):
    """The 2026-07-28 revision has no handshake, so no connection to remember a session for."""
    async with connect(hosted, "tok-de", mode="auto") as client:
        assert client.protocol_version == "2026-07-28"
        sid = (await client.call_tool("surfsky_session_start", {})).structured_content[
            "session_id"
        ]
        unnamed = error_of(
            await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        )
        named = await client.call_tool(
            "surfsky_navigate", {"url": "https://e.test/", "session_id": sid}
        )
    assert unnamed["code"] == "no_session" and "session_id" in unnamed["message"]
    assert named.structured_content["session_id"] == sid
