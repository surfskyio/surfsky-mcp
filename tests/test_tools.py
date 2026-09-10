import anyio
import pytest
from mcp.types import ImageContent
from surfsky import BrowserTimeoutError, ConfigurationError

from tests.conftest import FakePage, error_of, text_of

pytestmark = pytest.mark.anyio

NAMES = {
    "surfsky_status",
    "surfsky_scrape",
    "surfsky_session_start",
    "surfsky_session_stop",
    "surfsky_session_list",
    "surfsky_navigate",
    "surfsky_snapshot",
    "surfsky_act",
    "surfsky_wait",
    "surfsky_read",
    "surfsky_screenshot",
    "surfsky_evaluate",
    "surfsky_tab",
}
READ_ONLY = {
    "surfsky_status",
    "surfsky_scrape",
    "surfsky_session_list",
    "surfsky_snapshot",
    "surfsky_wait",
    "surfsky_read",
    "surfsky_screenshot",
}


async def test_tools_are_named_and_annotated(client):
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == NAMES
    for name, tool in tools.items():
        assert name in NAMES, name
        assert tool.title and tool.description and tool.annotations is not None
        assert tool.annotations.read_only_hint is (name in READ_ONLY), name
    assert tools["surfsky_session_stop"].annotations.idempotent_hint is True


async def test_descriptions_are_host_portable(client):
    for tool in (await client.list_tools()).tools:
        assert len(tool.description) <= 1024, (tool.name, len(tool.description))
        assert "\n    " not in tool.description and not tool.description.endswith("\n"), (
            tool.name
        )
        for name, prop in tool.input_schema.get("properties", {}).items():
            assert prop.get("description"), (tool.name, name)


async def test_status_reports_account_and_owned_sessions(client, fakes, monkeypatch):
    from surfsky_mcp import tools

    async def cloud(settings):
        return {
            "plan": "pro",
            "parallel_browsers": 5,
            "running": 1,
            "available": 4,
            "premium_proxy": "n/a (404)",
        }

    monkeypatch.setattr(tools, "cloud_status", cloud)
    await client.call_tool("surfsky_session_start", {})
    result = await client.call_tool("surfsky_status", {})
    data = result.structured_content
    assert data["token_source"] == "env" and data["base_url"] == "https://api.test"
    assert data["plan"] == "pro" and data["premium_proxy"] == "n/a (404)"
    assert (
        data["sessions"][0]["session_id"] == "S1"
        and data["sessions"][0]["default"] is False
    )
    assert "version: " in text_of(result)


async def test_missing_credentials_is_an_auth_error(fakes, monkeypatch):
    from mcp.client import Client

    from surfsky_mcp import server

    def broken():
        raise ConfigurationError("no API token")

    monkeypatch.setattr(server, "AsyncSurfsky", broken)
    async with Client(server.mcp) as fresh:
        error = error_of(await fresh.call_tool("surfsky_session_list", {}))
    assert error["code"] == "auth" and "SURFSKY_API_TOKEN" in error["hint"]


async def test_session_start_returns_handle_and_urls(client, fakes):
    result = await client.call_tool(
        "surfsky_session_start",
        {
            "proxy": "premium",
            "country": "us",
            "os": "android",
            "block_resources": ["image"],
            "idle_timeout": 120,
        },
    )
    data = result.structured_content
    assert data["session_id"] == "S1" and data["idle_timeout"] == 120
    assert data["screencast"].startswith("https://api.test/screencast?ws=")
    assert data["devtools"] == "https://api.test/S1/inspector"
    assert "bills per minute" in data["note"]
    started = fakes.client.profiles.started[0]
    assert started["proxy"].country == "us" and started["fingerprint"].os == "android"
    assert fakes.browsers[0].kwargs["block_resources"] == {"image"}


async def test_session_start_rejects_fingerprint_with_profile(client, fakes):
    error = error_of(
        await client.call_tool(
            "surfsky_session_start", {"profile_uuid": "P1", "os": "mac"}
        )
    )
    assert error["code"] == "usage" and "profile owns its fingerprint" in error["message"]
    assert fakes.client.profiles.started == []


async def test_session_start_rejects_targeting_with_proxy_url(client, fakes):
    error = error_of(
        await client.call_tool(
            "surfsky_session_start", {"proxy": "socks5://u:p@h:1", "country": "us"}
        )
    )
    assert error["code"] == "usage"


async def test_session_stop_is_idempotent_and_accepts_any_uuid(client, fakes):
    await client.call_tool("surfsky_session_start", {})
    first = await client.call_tool("surfsky_session_stop", {"session_id": "S1"})
    assert first.structured_content == {"session_id": "S1", "stopped": True}
    again = await client.call_tool("surfsky_session_stop", {"session_id": "S1"})
    assert again.structured_content["stopped"] is True
    fakes.client.profiles._session("S7")
    await client.call_tool("surfsky_session_stop", {"session_id": "S7"})
    assert "S7" in fakes.client.profiles.stopped


async def test_lifespan_exit_stops_owned_sessions(fakes, monkeypatch):
    from mcp.client import Client

    from surfsky_mcp import server

    monkeypatch.setattr(server, "AsyncSurfsky", lambda: fakes.client)
    async with Client(server.mcp) as mcp_client:
        started = await mcp_client.call_tool("surfsky_session_start", {})
        assert started.structured_content["session_id"] == "S1"
    assert fakes.client.profiles.stopped == ["S1"]
    assert fakes.client.closed


async def test_same_session_calls_serialise_and_stop_waits(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    browser = fakes.browsers[0]
    browser.fill_gate = anyio.Event()
    order = []

    async def fill():
        result = await client.call_tool(
            "surfsky_act",
            {"action": "fill", "target": "#q", "text": "hi", "snapshot": False},
        )
        order.append("fill")
        assert result.is_error is False

    async def stop():
        await anyio.sleep(0.05)
        result = await client.call_tool("surfsky_session_stop", {"session_id": "S1"})
        order.append("stop")
        assert result.is_error is False

    async with anyio.create_task_group() as tg:
        tg.start_soon(fill)
        tg.start_soon(stop)
        await anyio.sleep(0.15)
        assert order == []
        browser.fill_gate.set()
    assert order == ["fill", "stop"]


async def test_session_stop_failure_hint(client, fakes):
    from tests.conftest import server_error

    await client.call_tool("surfsky_session_start", {})
    fakes.client.profiles.fail_stop = server_error()
    error = error_of(await client.call_tool("surfsky_session_stop", {"session_id": "S1"}))
    assert "may keep billing" in error["hint"]


async def test_session_list_marks_owned(client, fakes):
    await client.call_tool("surfsky_session_start", {})
    fakes.client.profiles._session("S9", profile_uuid="P9")
    result = await client.call_tool("surfsky_session_list", {})
    rows = {row["session_id"]: row for row in result.structured_content["sessions"]}
    assert rows["S1"]["owned"] is True and rows["S9"]["owned"] is False
    assert rows["S9"]["profile_uuid"] == "P9" and rows["S1"]["profile_uuid"] is None
    assert result.structured_content["count"] == 2


async def test_navigate_auto_starts_default_and_fences_page_text(client, fakes):
    result = await client.call_tool("surfsky_navigate", {"url": "example.com"})
    text, data = text_of(result), result.structured_content
    assert fakes.browsers[0].called("goto")[0][1] == ("https://example.com",)
    assert data["session_id"] == "S1" and data["url"] == "https://example.com"
    assert data["status"] == 200 and data["navigated"] is True
    assert data["note"].startswith("started default session S1")
    assert "title" not in data and data["page"]["title"] == "Page https://example.com"
    assert data["page"]["snapshot"][0]["ref"] == 1
    head, fenced = text.split("<untrusted-page-content", 1)
    assert "title:" not in head and "title: Page https://example.com" in fenced
    assert '[@1] link "x" href=https://e.test/x' in fenced
    assert "note: started default session S1" in head
    second = await client.call_tool("surfsky_navigate", {"url": "https://e.test/b"})
    assert "note" not in second.structured_content


async def test_navigate_history_and_validation(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/a"})
    back = await client.call_tool("surfsky_navigate", {"go": "back", "snapshot": False})
    assert (
        back.structured_content["url"] == "https://e.test/prev"
        and back.structured_content["navigated"] is True
    )
    assert fakes.browsers[0].called("wait_for_load_state")
    assert "snapshot" not in back.structured_content["page"]
    end = error_of(await client.call_tool("surfsky_navigate", {"go": "forward"}))
    assert end["code"] == "usage" and "no further history" in end["message"]
    both = error_of(
        await client.call_tool("surfsky_navigate", {"url": "x", "go": "reload"})
    )
    assert both["code"] == "usage"
    neither = error_of(await client.call_tool("surfsky_navigate", {}))
    assert neither["code"] == "usage"


async def test_snapshot_lists_refs_and_reports_unreachable(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    fakes.browsers[0].walk = {
        "items": [{"ref": 1, "role": "button", "name": "Go", "selector": "#go"}],
        "total": 3,
        "unreachable": [
            {"kind": "iframe", "src": "https://pay.test/", "selector": "iframe"}
        ],
    }
    result = await client.call_tool(
        "surfsky_snapshot", {"interactive_only": True, "find": "go", "limit": 1}
    )
    text = text_of(result)
    assert (
        '[@1] button "Go"' in text
        and "[--] iframe src=https://pay.test/ (content not reachable)" in text
    )
    assert "(2 more; surfsky_snapshot with a higher limit lists them)" in text
    walk = fakes.browsers[0].called("evaluate")[-1]
    assert walk[1][1:] == (1, True, "go")
    assert result.structured_content["page"]["total"] == 3


async def test_read_formats_selector_and_truncation(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    md = await client.call_tool("surfsky_read", {})
    assert md.structured_content["format"] == "markdown"
    assert "# Hi" in md.structured_content["page"]["content"] and "# Hi" in text_of(md)
    links = await client.call_tool("surfsky_read", {"format": "links"})
    assert links.structured_content["page"]["content"] == "https://e.test/x"
    text = await client.call_tool("surfsky_read", {"format": "text", "selector": "#main"})
    assert text.structured_content["page"]["content"] == "text of #main"
    fakes.browsers[0].counts["#none"] = 0
    missing = error_of(await client.call_tool("surfsky_read", {"selector": "#none"}))
    assert missing["code"] == "not_found"
    cut = await client.call_tool("surfsky_read", {"format": "html", "max_chars": 20})
    assert cut.structured_content["truncated"] is True and "[truncated: " in text_of(cut)


async def test_screenshot_returns_image_block(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    result = await client.call_tool("surfsky_screenshot", {})
    assert (
        isinstance(result.content[1], ImageContent)
        and result.content[1].mime_type == "image/jpeg"
    )
    assert (
        result.structured_content["format"] == "jpeg"
        and result.structured_content["bytes"] == 9
    )
    assert fakes.browsers[0].called("bring_to_front")
    shot = fakes.browsers[0].called("screenshot")[0][2]
    assert shot == {"selector": None, "full_page": False, "format": "jpeg", "quality": 60}
    full = await client.call_tool("surfsky_screenshot", {"full_page": True})
    assert full.content[1].mime_type == "image/png"


async def test_evaluate_returns_json_and_uses_isolated_world(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    fakes.browsers[0].evaluate_result = {"a": [1, 2]}
    result = await client.call_tool(
        "surfsky_evaluate", {"expression": "(x) => x", "args": [1]}
    )
    assert result.structured_content["page"]["result"] == {"a": [1, 2]}
    assert '{"a": [1, 2]}' in text_of(result)
    call = fakes.browsers[0].called("evaluate")[-1]
    assert call[1] == ("(x) => x", 1) and call[2] == {"isolated": True}
    await client.call_tool("surfsky_evaluate", {"expression": "1", "main_world": True})
    assert fakes.browsers[0].called("evaluate")[-1][2] == {"isolated": False}


async def test_act_click_by_ref_reports_change(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    browser = fakes.browsers[0]
    browser.after_click_url = "https://e.test/next"
    result = await client.call_tool(
        "surfsky_act", {"action": "click", "target": "@1", "modifiers": ["Shift"]}
    )
    data = result.structured_content
    assert browser.called("click")[0] == (
        "click",
        ("a.first",),
        {"modifiers": ["Shift"], "timeout": 10.0},
    )
    assert data["navigated"] is True and data["changed"] is True and "hint" not in data
    assert data["page"]["snapshot"]  # refreshed


async def test_act_nothing_changed_hint(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    result = await client.call_tool("surfsky_act", {"action": "click", "target": "#same"})
    data = result.structured_content
    assert data["changed"] is False and data["hint"].startswith(
        "nothing observable changed"
    )
    assert "changed: false" in text_of(result)


async def test_act_validates_per_action(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    assert (
        error_of(
            await client.call_tool("surfsky_act", {"action": "fill", "target": "#q"})
        )["message"]
        == "fill needs text"
    )
    assert (
        error_of(await client.call_tool("surfsky_act", {"action": "press"}))["message"]
        == "press needs key"
    )
    assert (
        error_of(
            await client.call_tool("surfsky_act", {"action": "click", "target": "//a"})
        )["code"]
        == "usage"
    )


async def test_act_stale_ref_after_navigation(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/a"})
    fakes.browsers[0]._url = "https://e.test/b"  # the page moved on its own
    error = error_of(
        await client.call_tool("surfsky_act", {"action": "click", "target": "@1"})
    )
    assert (
        error["code"] == "stale_ref"
        and error["hint"] == "call surfsky_snapshot and pick the target again"
    )
    assert "page is now https://e.test/b" in error["message"]


async def test_act_inputs(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    browser = fakes.browsers[0]
    await client.call_tool(
        "surfsky_act", {"action": "fill", "target": "#q", "text": "hi", "snapshot": False}
    )
    assert browser.called("fill")[0][1] == ("#q", "hi")
    await client.call_tool(
        "surfsky_act",
        {"action": "type", "target": "@focused", "text": "more", "snapshot": False},
    )
    assert browser.keyboard.called("type")[0][1] == ("more",)
    await client.call_tool(
        "surfsky_act", {"action": "press", "key": "Enter", "snapshot": False}
    )
    assert browser.keyboard.called("press")[0] == (
        "press",
        ("Enter",),
        {"modifiers": None},
    )
    await client.call_tool(
        "surfsky_act",
        {
            "action": "select",
            "target": "#s",
            "value": "Two",
            "by_label": True,
            "snapshot": False,
        },
    )
    assert browser.called("select_option")[0] == (
        "select_option",
        ("#s", None),
        {"label": "Two"},
    )
    await client.call_tool(
        "surfsky_act", {"action": "hover", "target": "#h", "snapshot": False}
    )
    assert browser.called("hover")[0][1] == ("#h",)
    await client.call_tool("surfsky_act", {"action": "scroll", "snapshot": False})
    scrolled = browser.called("scroll")[0][2]["delta_y"]
    assert 600 <= scrolled <= 900
    await client.call_tool(
        "surfsky_act", {"action": "scroll", "target": "#footer", "snapshot": False}
    )
    assert browser.called("scroll_into_view")[0][1] == ("#footer",)


async def test_act_select_missing_option_is_not_found(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    browser = fakes.browsers[0]

    async def fail_select(selector, value=None, *, label=None):
        raise ValueError("no option")

    browser.select_option = fail_select
    error = error_of(
        await client.call_tool(
            "surfsky_act",
            {"action": "select", "target": "#s", "value": "nope", "snapshot": False},
        )
    )
    assert error["code"] == "not_found"


async def test_act_action_timeout(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    browser = fakes.browsers[0]
    browser.fill_gate = anyio.Event()  # never set: fill() hangs forever
    error = error_of(
        await client.call_tool(
            "surfsky_act",
            {
                "action": "fill",
                "target": "#q",
                "text": "x",
                "timeout": 0.05,
                "snapshot": False,
            },
        )
    )
    assert error["code"] == "timeout"


async def test_wait_conditions(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    browser = fakes.browsers[0]
    assert error_of(await client.call_tool("surfsky_wait", {}))["code"] == "usage"
    await client.call_tool("surfsky_wait", {"selector": "#x", "timeout": 5})
    assert browser.called("wait_for_selector")[0] == (
        "wait_for_selector",
        ("#x",),
        {"timeout": 5.0},
    )
    await client.call_tool("surfsky_wait", {"selector": "#spin", "gone": True})
    assert "getClientRects" in browser.called("wait_for_function")[0][1][0]
    await client.call_tool("surfsky_wait", {"url_contains": "/done", "text": "Thanks"})
    assert browser.called("wait_for_url")[0][1] == ("/done",)
    assert browser.called("wait_for_function")[-1][1][1] == "Thanks"
    result = await client.call_tool("surfsky_wait", {"seconds": 0.01})
    assert result.structured_content["url"] == "https://e.test/"


async def test_tabs(client, fakes):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    browser = fakes.browsers[0]
    new = await client.call_tool("surfsky_tab", {"action": "new", "url": "e.test/two"})
    assert new.structured_content["tab"] == "T2" and browser.pages[1].called("goto")[0][
        1
    ] == ("https://e.test/two",)
    listed = await client.call_tool("surfsky_tab", {"action": "list"})
    rows = listed.structured_content["page"]["tabs"]
    assert [row["active"] for row in rows] == [False, True] and rows[0]["id"] == "T1"
    switched = await client.call_tool("surfsky_tab", {"action": "switch", "tab": "0"})
    assert switched.structured_content["tab"] == "T1" and browser.called("bring_to_front")
    own = error_of(await client.call_tool("surfsky_tab", {"action": "close"}))
    assert own["code"] == "usage" and "first tab stays open" in own["message"]
    closed = await client.call_tool("surfsky_tab", {"action": "close", "tab": "T2"})
    assert closed.structured_content["closed"] == "T2" and browser.pages[1].closed
    assert (
        error_of(await client.call_tool("surfsky_tab", {"action": "switch"}))["code"]
        == "usage"
    )


async def test_tabs_switch_by_id_that_looks_like_a_long_number(client, fakes):
    from tests.conftest import FakePage

    await client.call_tool("surfsky_navigate", {"url": "https://e.test/"})
    browser = fakes.browsers[0]
    browser.pages.append(FakePage(target_id="12345678"))
    switched = await client.call_tool(
        "surfsky_tab", {"action": "switch", "tab": "12345678"}
    )
    assert switched.structured_content["tab"] == "12345678"


async def test_scrape_uses_a_throwaway_browser(client, fakes):
    result = await client.call_tool(
        "surfsky_scrape",
        {
            "url": "example.com",
            "formats": ["markdown", "links", "screenshot"],
            "proxy": "premium",
            "country": "de",
            "os": "mac",
            "wait_for": "#ready",
        },
    )
    data = result.structured_content
    browser = fakes.client.browsers[0]
    assert (
        browser.kwargs["proxy"].country == "de"
        and browser.kwargs["fingerprint"].os == "mac"
    )
    assert browser.called("goto")[0][1] == ("https://example.com",)
    assert browser.called("wait_for_selector")[0][1] == ("#ready",)
    assert browser.stopped and fakes.client.profiles.started == []  # no registry session
    assert data["url"] == "https://example.com" and data["status"] == 200
    assert (
        "# Hi" in data["page"]["markdown"]
        and data["page"]["links"] == "https://example.com/x"
    )
    assert "session_id" not in data
    assert (
        isinstance(result.content[1], ImageContent)
        and result.content[1].mime_type == "image/jpeg"
    )
    text = text_of(result)
    assert (
        "## markdown" in text
        and "## links" in text
        and "title:" in text.split("<untrusted-page-content")[1]
    )


async def test_scrape_defaults_and_truncation(client, fakes):
    result = await client.call_tool(
        "surfsky_scrape", {"url": "https://e.test/", "max_chars": 5}
    )
    data = result.structured_content
    assert list(data["page"]) == ["source", "title", "markdown"]
    assert (
        data["truncated"] == ["markdown"] and "[truncated: " in data["page"]["markdown"]
    )
    assert "## markdown" not in text_of(result)  # one format: no section heading
    profile = await client.call_tool(
        "surfsky_scrape", {"url": "https://e.test/", "profile_uuid": "P1"}
    )
    assert fakes.client.browsers[-1].kwargs["profile_uuid"] == "P1"
    assert profile.is_error is False


async def test_scrape_rejects_targeting_with_proxy_url(client, fakes):
    error = error_of(
        await client.call_tool(
            "surfsky_scrape",
            {"url": "https://e.test/", "proxy": "http://h:1", "city": "x"},
        )
    )
    assert error["code"] == "usage" and fakes.client.browsers == []


async def test_scrape_rejects_fingerprint_with_profile(client, fakes):
    error = error_of(
        await client.call_tool(
            "surfsky_scrape",
            {"url": "https://e.test/", "profile_uuid": "P1", "os": "mac"},
        )
    )
    assert error["code"] == "usage" and "profile owns its fingerprint" in error["message"]
    assert fakes.client.browsers == []


async def test_scrape_one_text_format_plus_screenshot_has_no_heading(client, fakes):
    result = await client.call_tool(
        "surfsky_scrape",
        {"url": "https://e.test/", "formats": ["markdown", "screenshot"]},
    )
    text = text_of(result)
    assert "# Hi" in text and "## markdown" not in text
    assert (
        isinstance(result.content[1], ImageContent)
        and result.content[1].mime_type == "image/jpeg"
    )


def timing_out(committed: bool):
    async def goto(self, url, **kwargs):
        if committed:
            self._url, self._title = url, "Page " + url
        raise BrowserTimeoutError(
            f"navigation to {url} did not reach 'load' within 30.0s"
        )

    return goto


async def test_scrape_keeps_a_page_whose_load_event_never_lands(client, monkeypatch):
    monkeypatch.setattr(FakePage, "goto", timing_out(committed=True))
    result = await client.call_tool("surfsky_scrape", {"url": "https://slow.test"})
    assert not result.is_error, text_of(result)
    data = result.structured_content
    assert "partly loaded" in data["note"] and data["page"]["markdown"]


async def test_scrape_fails_when_no_document_arrives(client, monkeypatch):
    monkeypatch.setattr(FakePage, "goto", timing_out(committed=False))
    result = await client.call_tool("surfsky_scrape", {"url": "https://dead.test"})
    assert error_of(result)["code"] == "timeout"


async def test_navigate_keeps_a_page_whose_load_event_never_lands(client, monkeypatch):
    await client.call_tool("surfsky_navigate", {"url": "https://e.test"})
    monkeypatch.setattr(FakePage, "goto", timing_out(committed=True))
    result = await client.call_tool("surfsky_navigate", {"url": "https://slow.test"})
    assert not result.is_error, text_of(result)
    header = result.structured_content
    assert "partly loaded" in header["note"] and header["navigated"] is True
