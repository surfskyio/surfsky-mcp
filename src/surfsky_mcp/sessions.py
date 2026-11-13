import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp.server.auth.provider import AccessToken
from surfsky import (
    AsyncSurfsky,
    AuthenticationError,
    Browser,
    ForbiddenError,
    Session,
)
from surfsky_cli import config
from surfsky_cli import session as cli
from surfsky_cli.out import Hinted, NoSession, SessionGone
from surfsky_cli.session import Attached

logger = logging.getLogger("surfsky_mcp")

DEFAULT_IDLE_TIMEOUT = 300  # seconds without a tool call
REAP_INTERVAL = 30.0
VERIFIED_TTL = 300.0
REFUSED_TTL = 60.0
MAX_VERIFIED = 10_000


@dataclass
class BrowserSession(Attached):
    """One session this server owns. `browser` and `page` are None until connected."""

    lock: anyio.Lock = field(default_factory=anyio.Lock)
    default: bool = False
    owner: str | None = None
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
    def __init__(
        self, client_factory: Callable[[], AsyncSurfsky], account: str | None = None
    ) -> None:
        self._factory = client_factory
        self.account = account
        self.last_used = time.time()
        self.current: dict[str, str] = {}
        self._client: AsyncSurfsky | None = None
        self.sessions: dict[str, BrowserSession] = {}
        self._default_lock = anyio.Lock()

    @property
    def hosted(self) -> bool:
        return self.account is not None

    def client(self) -> AsyncSurfsky:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def current_for(self, connection: str | None) -> str | None:
        if connection:
            return self.current.get(connection)
        unowned = [live.id for live in self.sessions.values() if live.owner is None]
        return unowned[0] if self.hosted and len(unowned) == 1 else None

    async def start(
        self,
        *,
        default: bool = False,
        owner: str | None = None,
        profile_uuid: str | None = None,
        idle_timeout: int | None = None,
        dialogs: str = "dismiss",
        **options: Any,
    ) -> BrowserSession:
        record = await cli.start(
            self.client(),
            save=not self.hosted,
            profile_uuid=profile_uuid,
            idle_timeout=idle_timeout or DEFAULT_IDLE_TIMEOUT,
            dialogs=dialogs,
            **options,
        )
        live = BrowserSession(
            record,
            {"last_used": time.time()},
            None,
            None,
            default=default,
            owner=owner,
        )
        self.sessions[live.id] = live
        if owner:
            self.current[owner] = live.id
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
        if session_id is None and self.hosted:
            raise NoSession(
                "no current session: pass session_id, or call surfsky_session_start first"
            )
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
            raise SessionGone(
                f"session {session_id} is not running on this server: stopped, idle timeout, or a restart"
            )
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
            self._forget(live)

    def _forget(self, live: BrowserSession) -> None:
        self.sessions.pop(live.id, None)
        self.current = {c: s for c, s in self.current.items() if s != live.id}

    def remove(self, live: BrowserSession) -> None:
        self._forget(live)
        config.delete_session(live.id)

    async def stop_all(self) -> None:
        for live in list(self.sessions.values()):
            try:
                await self._stop(live)
            except Exception as exc:
                self._warn(live, exc)

    def _warn(self, live: BrowserSession, exc: Exception) -> None:
        whose = f" of {self.account}" if self.account else ""
        logger.warning(f"session {live.id}{whose} may keep billing: {exc}")

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
                self._warn(live, exc)
            finally:
                live.lock.release()
        return reaped

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class Hosted:
    def __init__(
        self,
        client_factory: Callable[..., AsyncSurfsky],
        lookup_url: str,
        cluster_urls: dict[str, str],
    ) -> None:
        self._factory = client_factory
        self._lookup_url = lookup_url
        self._cluster_urls = cluster_urls
        self._verified: OrderedDict[str, tuple[float, AccessToken | None]] = OrderedDict()
        self.registries: dict[tuple[str, str], SessionRegistry] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        """The MCP SDK's TokenVerifier: None refuses the request with 401."""
        now = time.time()
        expires, access = self._verified.get(token, (0.0, None))
        if expires > now:
            self._verified.move_to_end(token)
            return access
        access = await self._look_up(token)
        if len(self._verified) >= MAX_VERIFIED:
            self._verified.popitem(last=False)  # the least recently used
        # an account without an API host is asked again soon: its region may just be chosen
        served = access is not None and access.claims and access.claims["base_url"]
        self._verified[token] = (now + (VERIFIED_TTL if served else REFUSED_TTL), access)
        return access

    async def _look_up(self, token: str) -> AccessToken | None:
        async with self._factory(api_token=token, base_url=self._lookup_url) as client:
            try:
                region = await client.account.region()
            except (AuthenticationError, ForbiddenError):
                return None
        # a dedicated host set on the account, else the host of its cluster
        base_url = region.url or self._cluster_urls.get(region.cluster or "")
        return AccessToken(
            token=token,
            client_id=hashlib.sha256(token.encode()).hexdigest()[:12],
            scopes=[],
            claims={"base_url": base_url, "cluster": region.cluster, "api_token": token},
        )

    def registry(self, access: AccessToken) -> SessionRegistry:
        claims = access.claims or {}
        token, base_url = claims["api_token"], claims.get("base_url")
        if not base_url and claims.get("cluster"):
            raise Hinted(f"this server does not serve cluster {claims['cluster']}")
        if not base_url:
            raise Hinted(
                "this account has no region yet: choose one at app.surfsky.io, then retry"
            )
        # a moved account gets a new registry; the old one is dropped once idle
        registry = self.registries.get((token, base_url))
        if registry is None:
            registry = self.registries[token, base_url] = SessionRegistry(
                lambda: self._factory(api_token=token, base_url=base_url),
                account=hashlib.sha256(token.encode()).hexdigest()[:12],
            )
        registry.last_used = time.time()
        return registry

    async def reap(self) -> None:
        while True:
            await anyio.sleep(REAP_INTERVAL)
            for key, registry in list(self.registries.items()):
                await registry.reap_once()
                idle = time.time() - registry.last_used
                if not registry.sessions and idle > DEFAULT_IDLE_TIMEOUT:
                    del self.registries[key]
                    await registry.aclose()

    async def stop_all(self) -> None:
        for registry in list(self.registries.values()):
            await registry.stop_all()

    async def aclose(self) -> None:
        for registry in list(self.registries.values()):
            await registry.aclose()
