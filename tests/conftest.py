from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from surfsky import Browser, Inspector, NotFoundError, ServerError, Session
from surfsky.browser.actions import Keyboard, Mouse

HTML = '<html><body><h1>Hi</h1><p>Text <a href="/x">x</a></p></body></html>'
WALK = {
    "items": [
        {
            "ref": 1,
            "role": "link",
            "name": "x",
            "selector": "a.first",
            "href": "https://e.test/x",
        }
    ],
    "total": 1,
    "unreachable": [],
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        assert (
            hasattr(Browser, name) or hasattr(Keyboard, name) or hasattr(Mouse, name)
        ), name

        async def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return method

    def called(self, name):
        return [call for call in self.calls if call[0] == name]


class FakePage(Recorder):
    def __init__(self, target_id="T1", url="about:blank", title=""):
        super().__init__()
        self.target_id = target_id
        self.closed = False
        self.status = 200
        self._url = url
        self._title = title
        self.walk = dict(WALK)
        self.evaluate_result = "ok"
        self.counts = {}
        self.after_click_url = None
        self.keyboard = Recorder()
        self.mouse = Recorder()
        self.fill_gate = None

    async def url(self):
        return self._url

    async def title(self):
        return self._title

    async def content(self):
        return HTML

    async def goto(self, url, **kwargs):
        self.calls.append(("goto", (url,), kwargs))
        self._url = url
        self._title = "Page " + url

    async def reload(self, **kwargs):
        self.calls.append(("reload", (), kwargs))

    async def wait_for_load_state(self, state="load", *, timeout=30.0):
        self.calls.append(("wait_for_load_state", (state,), {"timeout": timeout}))

    async def click(self, selector, **kwargs):
        self.calls.append(("click", (selector,), kwargs))
        if self.after_click_url:
            self._url = self.after_click_url

    async def fill(self, selector, text):
        if self.fill_gate is not None:
            await self.fill_gate.wait()
        self.calls.append(("fill", (selector, text), {}))

    async def evaluate(self, expression, *args, **kwargs):
        self.calls.append(("evaluate", (expression, *args), kwargs))
        if expression.startswith("(limit, interactive, find)"):
            return self.walk
        return self.evaluate_result

    async def screenshot(self, **kwargs):
        self.calls.append(("screenshot", (), kwargs))
        return b"\x89PNG-fake"

    async def inner_text(self, selector):
        return f"text of {selector}" if await self.count(selector) else None

    async def outer_html(self, selector):
        return f"<div>{selector}</div>" if await self.count(selector) else None

    async def count(self, selector):
        return self.counts.get(selector, 1)

    async def go_back(self, **kwargs):
        self._url = "https://e.test/prev"
        return self._url

    async def go_forward(self, **kwargs):
        return None

    async def select_option(self, selector, value=None, *, label=None):
        self.calls.append(("select_option", (selector, value), {"label": label}))
        return value or label

    async def send(self, method, params=None):
        self.calls.append(("send", (method, params), {}))
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientHeight": 1000}}
        return {}

    async def close(self):
        self.closed = True


class FakeBrowser(FakePage):
    def __init__(self, session, **kwargs):
        super().__init__(target_id="T1")
        self.session = session
        self.kwargs = kwargs
        self.pages = [self]
        self.connected = False
        self.on_dialog = None
        self.cdp = Recorder()
        self.connect_error = None
        self.connect_gate = None

    async def connect(self):
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def close(self):
        self.connected = False

    async def new_page(self):
        page = FakePage(target_id=f"T{len(self.pages) + 1}")
        self.pages.append(page)
        return page


class FakeProfiles:
    def __init__(self):
        self.active = []
        self.started = []
        self.stopped = []
        self.fail_stop = None
        self.stop_gate = None

    def _session(self, uuid, profile_uuid=None):
        self.active.append(
            SimpleNamespace(
                internal_uuid=uuid,
                profile_uuid=profile_uuid,
                one_time=profile_uuid is None,
                started_at="2026-09-09T10:00:00Z",
                active_seconds=5,
            )
        )
        return Session(
            internal_uuid=uuid,
            ws_url=f"wss://api.test/proxy/{uuid}",
            inspector=Inspector.model_validate(
                {
                    "list": f"https://api.test/{uuid}/inspector",
                    "screencast": f"wss://api.test/sc/{uuid}?t=x",
                }
            ),
        )

    async def start_one_time(self, **options):
        self.started.append(options)
        return self._session(f"S{len(self.started)}")

    async def start(self, uuid, **options):
        self.started.append({"profile_uuid": uuid, **options})
        return self._session(f"S{len(self.started)}", profile_uuid=uuid)

    async def list_active(self):
        return list(self.active)

    async def stop(self, uuid):
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.fail_stop is not None:
            raise self.fail_stop
        if not any(item.internal_uuid == uuid for item in self.active):
            raise NotFoundError("no such session", status_code=404)
        self.active = [item for item in self.active if item.internal_uuid != uuid]
        self.stopped.append(uuid)


class FakeClient:
    def __init__(self):
        self.profiles = FakeProfiles()
        self.base_url = "https://api.test"
        self.closed = False
        self.browsers = []

    def with_options(self, **_):
        return self

    async def aclose(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    @asynccontextmanager
    async def browser(self, **kwargs):
        browser = FakeBrowser(None, **kwargs)
        browser.connected = True
        self.browsers.append(browser)
        yield browser
        browser.stopped = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SURFSKY_HOME", str(tmp_path))
    monkeypatch.setenv("SURFSKY_API_TOKEN", "test-token")
    monkeypatch.setenv("SURFSKY_API_BASE_URL", "https://api.test")
    return tmp_path


@pytest.fixture
def fakes(env, monkeypatch):
    from surfsky_mcp import sessions

    client = FakeClient()
    made = []

    def make_browser(session, **kwargs):
        browser = FakeBrowser(session, **kwargs)
        made.append(browser)
        return browser

    monkeypatch.setattr(sessions, "Browser", make_browser)
    return SimpleNamespace(client=client, browsers=made)


def server_error():
    return ServerError("boom", status_code=500)


@pytest.fixture
async def client(fakes, monkeypatch):
    from mcp.client import Client

    from surfsky_mcp import server

    monkeypatch.setattr(server, "AsyncSurfsky", lambda: fakes.client)
    async with Client(server.mcp) as mcp_client:
        yield mcp_client


def text_of(result):
    return result.content[0].text


def error_of(result):
    assert result.is_error, text_of(result)
    return result.structured_content["error"]
