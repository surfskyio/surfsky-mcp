import time

import anyio
import pytest
from surfsky_cli import config
from surfsky_cli.out import NoSession, SessionGone

from surfsky_mcp.sessions import SessionRegistry
from tests.conftest import server_error

pytestmark = pytest.mark.anyio


def registry_for(fakes):
    return SessionRegistry(lambda: fakes.client)


async def test_default_session_is_started_once_under_concurrency(fakes):
    registry = registry_for(fakes)
    seen = []

    async def use():
        async with registry.acquire(None) as live:
            seen.append(live.id)

    async with anyio.create_task_group() as tg:
        tg.start_soon(use)
        tg.start_soon(use)
    assert seen == ["S1", "S1"]
    assert len(fakes.client.profiles.started) == 1
    live = registry.sessions["S1"]
    assert live.default and live.note.startswith("started default session S1")
    assert fakes.browsers[0].connected and fakes.browsers[0].on_dialog is not None
    assert config.load_session("S1")["idle_timeout"] == 300


async def test_explicit_start_forwards_options(fakes):
    registry = registry_for(fakes)
    live = await registry.start(
        profile_uuid="P1", idle_timeout=60, dialogs="accept", proxy="socks5://u:p@h:1"
    )
    assert live.id == "S1" and not live.default and live.idle_timeout == 60
    started = fakes.client.profiles.started[0]
    assert started["profile_uuid"] == "P1" and started["proxy"] == "socks5://u:p@h:1"
    assert started["browser_settings"].inactive_kill_timeout == 60


async def test_connect_failure_rolls_back_and_reraises(fakes, monkeypatch):
    from surfsky_mcp import sessions

    registry = registry_for(fakes)
    original = sessions.Browser

    def make(session, **kwargs):
        browser = original(session, **kwargs)
        browser.connect_error = ConnectionError("socket refused")
        return browser

    monkeypatch.setattr(sessions, "Browser", make)
    with pytest.raises(ConnectionError):
        await registry.start()
    assert fakes.client.profiles.stopped == ["S1"]
    assert registry.sessions == {}
    assert config.load_session("S1") is None


async def test_cancel_during_connect_still_stops(fakes, monkeypatch):
    from surfsky_mcp import sessions

    gate = anyio.Event()
    original = sessions.Browser

    def make(session, **kwargs):
        browser = original(session, **kwargs)
        browser.connect_gate = gate
        return browser

    monkeypatch.setattr(sessions, "Browser", make)
    registry = registry_for(fakes)
    async with anyio.create_task_group() as tg:
        tg.start_soon(registry.start)
        await anyio.sleep(0.05)
        assert "S1" in registry.sessions
        tg.cancel_scope.cancel()
    assert fakes.client.profiles.stopped == ["S1"]
    assert registry.sessions == {}


async def test_failed_rollback_keeps_dead_entry_for_shutdown(fakes, monkeypatch):
    from surfsky_mcp import sessions

    original = sessions.Browser

    def make(session, **kwargs):
        browser = original(session, **kwargs)
        browser.connect_error = ConnectionError("refused")
        return browser

    monkeypatch.setattr(sessions, "Browser", make)
    fakes.client.profiles.fail_stop = server_error()
    registry = registry_for(fakes)
    with pytest.raises(ConnectionError):
        await registry.start()
    assert registry.sessions["S1"].stop_failed
    fakes.client.profiles.fail_stop = None
    await registry.stop_all()
    assert fakes.client.profiles.stopped == ["S1"] and registry.sessions == {}


async def test_dropped_socket_reconnects_when_still_active(fakes):
    registry = registry_for(fakes)
    async with registry.acquire(None):
        pass
    fakes.browsers[0].connected = False
    async with registry.acquire(None) as live:
        assert live.browser is fakes.browsers[1] and live.browser.connected


async def test_dropped_socket_is_gone_when_not_active(fakes):
    registry = registry_for(fakes)
    async with registry.acquire(None):
        pass
    fakes.browsers[0].connected = False
    fakes.client.profiles.active.clear()
    with pytest.raises(SessionGone, match="idle timeout, or stopped"):
        async with registry.acquire(None):
            pass
    assert registry.sessions == {} and config.load_session("S1") is None
    async with registry.acquire(None) as live:
        assert live.id == "S2"


async def test_first_tab_closed_is_gone(fakes):
    registry = registry_for(fakes)
    async with registry.acquire(None):
        pass
    fakes.browsers[0].closed = True
    with pytest.raises(SessionGone, match="first tab closed or crashed"):
        async with registry.acquire("S1"):
            pass
    assert fakes.client.profiles.stopped == ["S1"]


async def test_closed_active_tab_falls_back_to_first_open_page(fakes):
    registry = registry_for(fakes)
    async with registry.acquire(None) as live:
        second = await live.browser.new_page()
        live.page = second
    second.closed = True
    async with registry.acquire("S1") as live:
        assert live.page is fakes.browsers[0]


async def test_unknown_session_id(fakes):
    registry = registry_for(fakes)
    with pytest.raises(NoSession, match="not started by this server"):
        async with registry.acquire("nope"):
            pass


async def test_stop_any_uuid_and_repeat(fakes):
    registry = registry_for(fakes)
    await registry.start()
    await registry.stop("S1")
    assert registry.sessions == {} and fakes.client.profiles.stopped == ["S1"]
    await registry.stop("S1")
    fakes.client.profiles._session("S9")
    await registry.stop("S9")
    assert "S9" in fakes.client.profiles.stopped


async def test_stop_failure_marks_dead_and_raises(fakes):
    registry = registry_for(fakes)
    await registry.start()
    fakes.client.profiles.fail_stop = server_error()
    with pytest.raises(Exception, match="boom"):
        await registry.stop("S1")
    assert registry.sessions["S1"].stop_failed
    with pytest.raises(SessionGone):
        async with registry.acquire("S1"):
            pass


async def test_reaper_stops_idle_sessions_but_not_busy_ones(fakes):
    registry = registry_for(fakes)
    idle = await registry.start(idle_timeout=100)
    busy = await registry.start(idle_timeout=100)
    now = time.time() + 101
    async with busy.lock:
        assert await registry.reap_once(now) == [idle.id]
    assert fakes.client.profiles.stopped == [idle.id]


async def test_reaper_holds_the_lock_for_the_whole_stop(fakes):
    registry = registry_for(fakes)
    live = await registry.start(idle_timeout=100)
    gate = anyio.Event()
    fakes.client.profiles.stop_gate = gate
    now = time.time() + 101
    async with anyio.create_task_group() as tg:
        tg.start_soon(registry.reap_once, now)
        await anyio.sleep(0.05)
        assert live.lock.locked()
        gate.set()
    assert fakes.client.profiles.stopped == [live.id]
    with pytest.raises(NoSession):
        async with registry.acquire(live.id):
            pass


async def test_stop_all_and_aclose(fakes):
    registry = registry_for(fakes)
    await registry.start()
    await registry.start()
    await registry.stop_all()
    await registry.aclose()
    assert sorted(fakes.client.profiles.stopped) == ["S1", "S2"] and fakes.client.closed
