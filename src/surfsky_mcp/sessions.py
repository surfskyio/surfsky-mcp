import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import anyio
from surfsky import AsyncSurfsky, Browser, Session
from surfsky_cli import config
from surfsky_cli import session as cli
from surfsky_cli.out import NoSession, SessionGone
from surfsky_cli.session import Attached

logger = logging.getLogger("surfsky_mcp")

DEFAULT_IDLE_TIMEOUT = 300  # seconds without a tool call
REAP_INTERVAL = 30.0


@dataclass
class BrowserSession(Attached):
    """One session this server owns. `browser` and `page` are None until connected."""

    lock: anyio.Lock = field(default_factory=anyio.Lock)
    default: bool = False
    last_used: float = field(default_factory=time.time)
    snapshot_hash: str | None = None
    stop_failed: bool = False
    note: str | None = None

    @property
    def id(self) -> str:
        return self.session["internal_uuid"]

    @property
    def idle_timeout(self) -> int:
        return self.session["idle_timeout"]


class SessionRegistry:
    def __init__(self, client_factory: Callable[[], AsyncSurfsky]) -> None:
        self._factory = client_factory
        self._client: AsyncSurfsky | None = None
        self.sessions: dict[str, BrowserSession] = {}
        self._default_lock = anyio.Lock()

    def client(self) -> AsyncSurfsky:
        if self._client is None:
            self._client = self._factory()
        return self._client

    async def start(
        self,
        *,
        default: bool = False,
        profile_uuid: str | None = None,
        idle_timeout: int | None = None,
        dialogs: str = "dismiss",
        **options: Any,
    ) -> BrowserSession:
        record = await cli.start(
            self.client(),
            profile_uuid=profile_uuid,
            idle_timeout=idle_timeout or DEFAULT_IDLE_TIMEOUT,
            dialogs=dialogs,
            **options,
        )
        live = BrowserSession(
            record, {"last_used": time.time()}, None, None, default=default
        )
        self.sessions[live.id] = live
        try:
            async with live.lock:
                await self._connect(live)
        except BaseException:
            with anyio.CancelScope(shield=True), suppress(Exception):
                await self._stop(live)
            raise
        return live

    async def _connect(self, live: BrowserSession) -> None:
        record = live.session
        browser = Browser(
            Session(internal_uuid=live.id, ws_url=record["ws_url"]),
            block_resources=set(record.get("block_resources") or ()),
        )
        browser.on_dialog = cli.dialog_policy(
            record.get("dialogs") or "dismiss", live.dialogs
        )
        await browser.connect()
        live.browser = browser
        live.page = await cli.pick_tab(browser, live.state.get("target_id"))

    @asynccontextmanager
    async def acquire(self, session_id: str | None) -> AsyncIterator[BrowserSession]:
        live = (
            await self._default_session()
            if session_id is None
            else self._lookup(session_id)
        )
        async with live.lock:
            live.last_used = time.time()
            await self._ensure_connected(live)
            yield live
            live.last_used = time.time()

    def _lookup(self, session_id: str) -> BrowserSession:
        live = self.sessions.get(session_id)
        if live is None:
            raise NoSession(f"session {session_id} was not started by this server")
        return live

    async def _default_session(self) -> BrowserSession:
        async with self._default_lock:
            for live in self.sessions.values():
                if live.default and not live.stop_failed:
                    return live
            live = await self.start(default=True)
            live.note = (
                f"started default session {live.id}; bills until surfsky_session_stop "
                f"or {live.idle_timeout} s without a tool call"
            )
            return live

    async def _ensure_connected(self, live: BrowserSession) -> None:
        """Reconnect a dropped socket; report a session the cloud no longer has."""
        if live.stop_failed:
            raise SessionGone(
                f"session {live.id} could not be stopped earlier; start another"
            )
        browser = live.browser
        if browser is None or not browser.connected:
            fast = self.client().with_options(max_retries=0)
            active = {item.internal_uuid for item in await fast.profiles.list_active()}
            if live.id not in active:
                self.remove(live)
                raise SessionGone(f"session {live.id} is gone (idle timeout, or stopped)")
            if browser is not None:
                with suppress(Exception):
                    await browser.close()
            await self._connect(live)
            return
        if browser.closed:
            with suppress(Exception):
                await self._stop(live)
            raise SessionGone(
                f"session {live.id} is gone: its first tab closed or crashed"
            )
        if live.page.closed:
            live.page = next((page for page in browser.pages if not page.closed), browser)

    async def stop(self, session_id: str) -> None:
        """Stop any session on the account; a registry entry, if any, goes with it."""
        live = self.sessions.get(session_id)
        if live is None:
            live = BrowserSession(
                {"internal_uuid": session_id, "idle_timeout": 0}, {}, None, None
            )
        async with live.lock:
            await self._stop(live)

    async def _stop(self, live: BrowserSession) -> None:
        with anyio.CancelScope(shield=True):
            if live.browser is not None:
                with suppress(Exception):
                    await live.browser.close()
            try:
                await cli.stop(self.client(), live.id)
            except Exception:
                live.stop_failed = True
                self.sessions[live.id] = live
                raise
            self.sessions.pop(live.id, None)

    def remove(self, live: BrowserSession) -> None:
        self.sessions.pop(live.id, None)
        config.delete_session(live.id)

    async def stop_all(self) -> None:
        for live in list(self.sessions.values()):
            try:
                await self._stop(live)
            except Exception as exc:
                logger.warning("session %s may keep billing: %s", live.id, exc)

    async def reap(self) -> None:
        """Stop owned sessions idle past their timeout, measured in tool calls."""
        while True:
            await anyio.sleep(REAP_INTERVAL)
            await self.reap_once()

    async def reap_once(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        reaped: list[str] = []
        for live in list(self.sessions.values()):
            if live.lock.locked() or now - live.last_used <= live.idle_timeout:
                continue
            live.lock.acquire_nowait()
            try:
                await self._stop(live)
                reaped.append(live.id)
            except Exception as exc:
                logger.warning("session %s may keep billing: %s", live.id, exc)
            finally:
                live.lock.release()
        return reaped

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
