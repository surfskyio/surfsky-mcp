import base64
import hashlib
import json
import logging
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from surfsky_mcp import oauth, server
from surfsky_mcp.sessions import Hosted
from tests.conftest import error_of, header_of, text_of
from tests.test_hosted import ACCEPT, INIT, ApiClient, connect, http

pytestmark = pytest.mark.anyio

SECRET = "internal-secret-0123456789abcdef0123456789"
REDIRECT = "http://localhost:8787/callback"
VERIFIER = "v" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)


class WebApp:
    """web_app's internal routes, as contract.md describes them."""

    def __init__(self):
        self.hosts, self.apps, self.calls, self.raw_paths = {}, [], [], []
        self.down = False

    def allow(self, client_id, redirect_uri, api_token):
        """What Allow on the consent page does. Returns the one-time code."""
        app = {
            "id": len(self.apps) + 1,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "api_token": api_token,
            "code": f"one-time-code-{len(self.apps) + 1}",
            "refresh": None,
            "revoked": False,
        }
        self.apps.append(app)
        return app["code"]

    def _grant(self, app):
        app["refresh"] = f"refresh-{app['id']}-{len(self.calls)}"
        return httpx2.Response(
            200,
            json={
                "connected_app_id": app["id"],
                "api_token": app["api_token"],
                "refresh_token": app["refresh"],
            },
        )

    def handle(self, request):
        path = request.url.path.removeprefix("/internal/mcp")
        assert path != request.url.path
        self.calls.append(f"{request.method} {path}")
        self.raw_paths.append(request.url.raw_path.decode())
        if self.down:
            return httpx2.Response(503)
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx2.Response(401)
        body = json.loads(request.content) if request.content else {}
        if path == "/hosts":
            if len(request.content) > 16 * 1024:
                return httpx2.Response(413)
            self.hosts[body["client_id"]] = body
            return httpx2.Response(201, json={})
        if path.startswith("/hosts/"):
            host = self.hosts.get(path.removeprefix("/hosts/"))
            if host is None:
                return httpx2.Response(404, json={"code": "unknown_host"})
            return httpx2.Response(200, json=host)
        if path == "/redeem":
            for app in self.apps:
                if app["code"] == body["code"] and (
                    app["client_id"],
                    app["redirect_uri"],
                ) == (body["client_id"], body["redirect_uri"]):
                    app["code"] = None
                    return self._grant(app)
            return httpx2.Response(404, json={"code": "invalid_code"})
        if path == "/renew":
            for app in self.apps:
                if (
                    app["refresh"] == body["refresh_token"]
                    and app["client_id"] == body["client_id"]
                ):
                    if app["revoked"]:
                        return httpx2.Response(403, json={"code": "revoked"})
                    return self._grant(app)
            return httpx2.Response(403, json={"code": "unknown_token"})
        raise AssertionError(path)


def start(fake_web_app):
    """A hosted server with OAuth on. A second call is the server after a restart."""
    made, lookups = [], []

    def make_client(api_token=None, base_url=None):
        made.append(ApiClient(api_token, base_url, lookups))
        return made[-1]

    accounts = Hosted(
        make_client,
        "https://api-de2.test",
        {"de2": "https://api-de2.test", "us1": "https://api-us1.test"},
    )
    provider = oauth.Provider(
        accounts,
        "https://mcp.test",
        "https://app.test/connect",
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(fake_web_app.handle),
            base_url="http://web-app.test",
            headers={"Authorization": f"Bearer {SECRET}"},
        ),
    )
    app = server.hosted_app(accounts, ["mcp.test"], oauth=provider)
    return SimpleNamespace(
        app=app, made=made, lookups=lookups, accounts=accounts, web_app=fake_web_app
    )


@pytest.fixture
async def hosted(fakes):
    running = start(WebApp())
    async with running.app.router.lifespan_context(running.app):
        yield running


async def register(client, redirect_uri=REDIRECT):
    reply = await client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "Cursor",
            "token_endpoint_auth_method": "none",
        },
    )
    assert reply.status_code == 201
    return reply.json()["client_id"]


async def authorize(client, client_id, **changes):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "st",
    } | changes
    return await client.get("/authorize", params=params)


def query_of(reply):
    assert reply.status_code == 302
    return {
        k: v[0] for k, v in parse_qs(urlsplit(reply.headers["location"]).query).items()
    }


async def consent(hosted, client, api_token="tok-de"):
    """Register, authorize and click Allow. Returns the client id and the callback query."""
    client_id = await register(client)
    asked = query_of(await authorize(client, client_id))
    code = hosted.web_app.allow(asked["client_id"], asked["redirect_uri"], api_token)
    return client_id, {"request": asked["request"], "code": code}


async def log_in(hosted, api_token="tok-de"):
    async with http(hosted.app) as client:
        client_id, back = await consent(hosted, client, api_token)
        returned = query_of(await client.get("/authorize/callback", params=back))
        assert returned["state"] == "st"
        reply = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": returned["code"],
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": VERIFIER,
            },
        )
    assert reply.status_code == 200, reply.text
    return client_id, reply.json()


async def renew(hosted, client_id, refresh_token):
    async with http(hosted.app) as client:
        return await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
        )


async def test_no_token_gets_401_pointing_to_the_login(hosted):
    async with http(hosted.app) as client:
        refused = await client.post("/mcp", json=INIT, headers=ACCEPT)
        pointer = "https://mcp.test/.well-known/oauth-protected-resource/mcp"
        assert refused.status_code == 401
        assert f'resource_metadata="{pointer}"' in refused.headers["www-authenticate"]
        resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
        issuer = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert resource["resource"] == "https://mcp.test/mcp"
    assert resource["authorization_servers"] == ["https://mcp.test"]
    assert issuer["issuer"] == "https://mcp.test"
    assert issuer["registration_endpoint"] == "https://mcp.test/register"
    assert issuer["code_challenge_methods_supported"] == ["S256"]
    assert "revocation_endpoint" not in issuer


async def test_registered_host_is_stored_in_web_app_and_survives_a_restart(hosted):
    async with http(hosted.app) as client:
        client_id = await register(client)
    assert hosted.web_app.hosts[client_id]["client_name"] == "Cursor"
    restarted = start(hosted.web_app)
    async with http(restarted.app) as client:
        assert (await authorize(client, client_id)).status_code == 302


async def test_client_id_cannot_reach_another_web_app_path(hosted):
    async with http(hosted.app) as client:
        reply = await authorize(client, "x/../../api/health")
    assert reply.status_code == 400
    assert hosted.web_app.raw_paths == ["/internal/mcp/hosts/x%2F..%2F..%2Fapi%2Fhealth"]


async def test_registration_too_large_for_web_app_is_invalid_metadata(hosted):
    async with http(hosted.app) as client:
        reply = await client.post(
            "/register",
            json={"redirect_uris": [REDIRECT], "client_name": "x" * 17_000},
        )
    assert reply.status_code == 400
    assert reply.json()["error"] == "invalid_client_metadata"
    assert hosted.web_app.hosts == {}


async def test_authorize_sends_the_browser_to_the_consent_page(hosted):
    async with http(hosted.app) as client:
        client_id = await register(client)
        reply = await authorize(client, client_id)
        asked = query_of(reply)
        assert reply.headers["location"].startswith("https://app.test/connect?")
        assert asked == {
            "request": asked["request"],
            "client_id": client_id,
            "redirect_uri": REDIRECT,
        }
        unknown_host = await authorize(client, "nobody")
        other_redirect = await authorize(
            client, client_id, redirect_uri="https://evil.test/cb"
        )
        no_pkce = query_of(
            await client.get(
                "/authorize",
                params={"response_type": "code", "client_id": client_id, "state": "st"},
            )
        )
    assert unknown_host.status_code == other_redirect.status_code == 400
    assert no_pkce["error"] == "invalid_request"


async def test_login_gives_the_host_its_own_tokens_and_tools_work(hosted):
    _, tokens = await log_in(hosted)
    assert tokens["access_token"].startswith("mcp_")
    assert 3500 < tokens["expires_in"] <= 3600
    assert tokens["refresh_token"] and "tok-de" not in json.dumps(tokens)
    async with connect(hosted, tokens["access_token"]) as client:
        result = await client.call_tool("surfsky_scrape", {"url": "e.test"})
        no_session = error_of(
            await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        )
        started = (await client.call_tool("surfsky_session_start", {})).structured_content
        moved = header_of(
            await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
        )
    assert "# Hi" in text_of(result)
    used = [(c.token, c.base_url) for c in hosted.made if c.scrapes]
    assert used == [("tok-de", "https://api-de2.test")]
    assert hosted.lookups == ["tok-de"]
    assert no_session["code"] == "no_session"
    assert moved["session_id"] == started["session_id"]


async def test_status_reports_the_account_behind_the_access_token(hosted, monkeypatch):
    from surfsky_mcp import tools

    seen = {}

    async def cloud_status(settings):
        seen.update(token=settings.token()[0], url=settings.url())
        return {"plan": "pro"}

    monkeypatch.setattr(tools, "cloud_status", cloud_status)
    _, tokens = await log_in(hosted, "tok-us")
    async with connect(hosted, tokens["access_token"]) as client:
        await client.call_tool("surfsky_status", {})
    assert seen == {"token": "tok-us", "url": "https://api-us1.test"}


async def test_account_without_a_region_gets_the_same_message(hosted):
    _, tokens = await log_in(hosted, "tok-new")
    async with connect(hosted, tokens["access_token"]) as client:
        error = error_of(await client.call_tool("surfsky_scrape", {"url": "e.test"}))
    assert "region" in error["message"] and "app.surfsky.io" in error["message"]


async def test_cancel_returns_access_denied_to_the_host(hosted):
    async with http(hosted.app) as client:
        _, back = await consent(hosted, client)
        reply = await client.get(
            "/authorize/callback",
            params={"request": back["request"], "error": "access_denied"},
        )
    assert reply.headers["location"].startswith(REDIRECT + "?")
    assert query_of(reply) == {"error": "access_denied", "state": "st"}
    assert "POST /redeem" not in hosted.web_app.calls


async def test_code_that_web_app_refuses_is_access_denied(hosted):
    async with http(hosted.app) as client:
        _, back = await consent(hosted, client)
        reply = await client.get("/authorize/callback", params=back | {"code": "made-up"})
    assert query_of(reply) == {"error": "access_denied", "state": "st"}


async def test_web_app_down_at_the_callback_is_a_server_error(hosted):
    async with http(hosted.app) as client:
        _, back = await consent(hosted, client)
        hosted.web_app.down = True
        reply = await client.get("/authorize/callback", params=back)
    assert query_of(reply) == {"error": "server_error", "state": "st"}


async def test_callback_works_once_and_only_for_a_known_request(hosted):
    async with http(hosted.app) as client:
        _, back = await consent(hosted, client)
        unknown = await client.get(
            "/authorize/callback", params=back | {"request": "made-up"}
        )
        assert (await client.get("/authorize/callback", params=back)).status_code == 302
        again = await client.get("/authorize/callback", params=back)
    assert unknown.status_code == again.status_code == 400
    assert again.text == "This login expired. Start again from your app."


async def test_pending_request_expires(hosted, monkeypatch):
    monkeypatch.setattr(oauth, "PENDING_TTL", -1)
    async with http(hosted.app) as client:
        _, back = await consent(hosted, client)
        assert (await client.get("/authorize/callback", params=back)).status_code == 400


async def test_authorization_code_works_once_and_needs_the_verifier(hosted):
    async with http(hosted.app) as client:
        client_id, back = await consent(hosted, client)
        code = query_of(await client.get("/authorize/callback", params=back))["code"]
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        }
        wrong = await client.post("/token", data=form | {"code_verifier": "w" * 43})
        first = await client.post("/token", data=form)
        second = await client.post("/token", data=form)
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    assert first.status_code == 200
    assert second.status_code == 400 and second.json()["error"] == "invalid_grant"


async def test_api_token_in_the_header_works_without_web_app(hosted):
    async with connect(hosted, "tok-de") as client:
        result = await client.call_tool("surfsky_scrape", {"url": "e.test"})
    assert "# Hi" in text_of(result)
    async with http(hosted.app, "tok-bad") as stranger:
        reply = await stranger.post("/mcp", json=INIT, headers=ACCEPT)
    assert reply.status_code == 401
    assert hosted.web_app.calls == []


async def test_renewal_returns_a_new_pair_and_kills_the_old_refresh_token(hosted):
    client_id, tokens = await log_in(hosted)
    reply = await renew(hosted, client_id, tokens["refresh_token"])
    renewed = reply.json()
    assert reply.status_code == 200
    assert renewed["access_token"] not in (tokens["access_token"], "tok-de")
    assert renewed["refresh_token"] != tokens["refresh_token"]
    old = await renew(hosted, client_id, tokens["refresh_token"])
    assert old.status_code == 400 and old.json()["error"] == "invalid_grant"
    async with connect(hosted, renewed["access_token"]) as client:
        result = await client.call_tool("surfsky_scrape", {"url": "e.test"})
    assert "# Hi" in text_of(result)


async def test_regenerated_api_key_is_picked_up_at_renewal(hosted):
    client_id, tokens = await log_in(hosted)
    hosted.web_app.apps[0]["api_token"] = "tok-own"
    renewed = (await renew(hosted, client_id, tokens["refresh_token"])).json()
    async with connect(hosted, renewed["access_token"]) as client:
        await client.call_tool("surfsky_scrape", {"url": "e.test"})
    assert [c.token for c in hosted.made if c.scrapes] == ["tok-own"]


async def test_revoked_connected_app_is_refused_at_renewal(hosted):
    client_id, tokens = await log_in(hosted)
    hosted.web_app.apps[0]["revoked"] = True
    reply = await renew(hosted, client_id, tokens["refresh_token"])
    assert reply.status_code == 400 and reply.json()["error"] == "invalid_grant"


async def test_web_app_down_at_renewal_does_not_cost_the_refresh_token(hosted):
    client_id, tokens = await log_in(hosted)
    hosted.web_app.down = True
    with pytest.raises(httpx2.HTTPStatusError):  # the token endpoint answers 500
        await renew(hosted, client_id, tokens["refresh_token"])
    hosted.web_app.down = False
    assert (await renew(hosted, client_id, tokens["refresh_token"])).status_code == 200


async def test_unknown_and_expired_access_tokens_get_401(hosted, monkeypatch):
    async with http(hosted.app, "mcp_made-up") as stranger:
        reply = await stranger.post("/mcp", json=INIT, headers=ACCEPT)
    assert reply.status_code == 401
    assert hosted.lookups == []  # never sent to the API as an API token
    monkeypatch.setattr(oauth, "ACCESS_TTL", -1)
    _, tokens = await log_in(hosted)
    async with http(hosted.app, tokens["access_token"]) as late:
        assert (await late.post("/mcp", json=INIT, headers=ACCEPT)).status_code == 401


async def test_after_a_restart_the_access_token_is_refused_and_renewal_works(hosted):
    client_id, tokens = await log_in(hosted)
    restarted = start(hosted.web_app)
    async with restarted.app.router.lifespan_context(restarted.app):
        async with http(restarted.app, tokens["access_token"]) as client:
            reply = await client.post("/mcp", json=INIT, headers=ACCEPT)
        assert reply.status_code == 401
        renewed = await renew(restarted, client_id, tokens["refresh_token"])
        assert renewed.status_code == 200
        async with connect(restarted, renewed.json()["access_token"]) as client:
            result = await client.call_tool("surfsky_scrape", {"url": "e.test"})
    assert "# Hi" in text_of(result)


async def test_sessions_belong_to_the_account_not_to_the_connected_app(hosted):
    _, mine = await log_in(hosted, "tok-de")
    _, theirs = await log_in(hosted, "tok-us")
    async with (
        connect(hosted, mine["access_token"]) as owner,
        connect(hosted, theirs["access_token"]) as other,
        connect(hosted, "tok-de") as same_account,
    ):
        sid = (await owner.call_tool("surfsky_session_start", {})).structured_content[
            "session_id"
        ]
        visit = {"url": "https://e.test/", "session_id": sid}
        denied = error_of(await other.call_tool("surfsky_navigate", visit))
        shared = header_of(await same_account.call_tool("surfsky_navigate", visit))
    assert denied["code"] == "session_gone"
    assert shared["session_id"] == sid


async def test_connected_apps_without_connections_keep_their_own_current_session(
    hosted,
):
    """Two hosts on the 2026-07-28 revision, logged in to one account."""
    _, first = await log_in(hosted, "tok-de")
    _, second = await log_in(hosted, "tok-de")
    visit = {"url": "https://e.test/"}
    async with (
        connect(hosted, first["access_token"], mode="auto") as one,
        connect(hosted, second["access_token"], mode="auto") as two,
        connect(hosted, "tok-de", mode="auto") as by_header,
    ):
        assert one.protocol_version == "2026-07-28"
        mine = (await one.call_tool("surfsky_session_start", {})).structured_content
        nothing_yet = error_of(await two.call_tool("surfsky_navigate", visit))
        theirs = (await two.call_tool("surfsky_session_start", {})).structured_content
        on_one = header_of(await one.call_tool("surfsky_navigate", visit))
        on_two = header_of(await two.call_tool("surfsky_navigate", visit))
        unclaimed = error_of(await by_header.call_tool("surfsky_navigate", visit))
        named = header_of(
            await two.call_tool(
                "surfsky_navigate", {**visit, "session_id": mine["session_id"]}
            )
        )
    assert nothing_yet["code"] == "no_session"  # the other app's session is not shared
    assert on_one["session_id"] == mine["session_id"] != theirs["session_id"]
    assert on_two["session_id"] == theirs["session_id"]
    assert unclaimed["code"] == "no_session"  # both sessions belong to an app
    assert named["session_id"] == mine["session_id"]  # an explicit id wins


async def test_tokens_codes_and_the_secret_stay_out_of_logs(hosted, caplog):
    caplog.set_level(logging.DEBUG)
    client_id, tokens = await log_in(hosted)
    renewed = (await renew(hosted, client_id, tokens["refresh_token"])).json()
    await renew(hosted, client_id, tokens["refresh_token"])
    async with connect(hosted, renewed["access_token"]) as client:
        sid = (await client.call_tool("surfsky_session_start", {})).structured_content[
            "session_id"
        ]
        api = next(c for c in hosted.made if c.profiles.started)
        api.profiles.fail_stop = RuntimeError("stop failed")
        await client.call_tool("surfsky_session_stop", {"session_id": sid})
        await hosted.accounts.stop_all()
    # the test's own requests to the server are the browser's and the host's, not ours
    logged = "\n".join(
        r.getMessage() for r in caplog.records if "http://mcp.test/" not in r.getMessage()
    )
    pairs = [
        t[kind] for t in (tokens, renewed) for kind in ("access_token", "refresh_token")
    ]
    leaked = [s for s in [SECRET, "tok-de", "one-time-code-1", *pairs] if s in logged]
    assert leaked == []
    assert hashlib.sha256(b"tok-de").hexdigest()[:12] in caplog.text


async def test_with_oauth_off_no_login_route_is_served(fakes):
    accounts = Hosted(lambda **_: None, "https://api-de2.test", {})
    app = server.hosted_app(accounts, ["mcp.test"])
    async with http(app) as client:
        refused = await client.post("/mcp", json=INIT, headers=ACCEPT)
        for path in (
            "/register",
            "/authorize",
            "/authorize/callback",
            "/token",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            assert (await client.get(path)).status_code == 404, path
    assert refused.status_code == 401
    assert "resource_metadata" not in refused.headers["www-authenticate"]


def test_oauth_switch_needs_its_settings(monkeypatch, capsys):
    monkeypatch.setenv("SURFSKY_API_BASE_URL", "https://api-de2.test")
    monkeypatch.setenv("SURFSKY_OAUTH", "1")
    monkeypatch.setenv("SURFSKY_MCP_URL", "https://mcp.test")
    monkeypatch.delenv("SURFSKY_MCP_INTERNAL_SECRET", raising=False)
    with pytest.raises(SystemExit):
        server.main(["--hosted"])
    assert "SURFSKY_MCP_INTERNAL_SECRET" in capsys.readouterr().err
