import base64
import functools
import hashlib
import inspect
import json
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from typing import Annotated, Any, Literal, cast

import anyio
import click
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from surfsky import BrowserTimeoutError, SurfskyError
from surfsky.types import ScreenshotOptions
from surfsky_cli import html, snapshot
from surfsky_cli import session as cli
from surfsky_cli.commands.scrape import build, with_scheme
from surfsky_cli.commands.status import cloud_status
from surfsky_cli.config import Settings
from surfsky_cli.out import NotFound, StaleRef, render
from surfsky_cli.snapshot import wait_for_load

from . import __version__
from .output import (
    DEFAULT_MAX_CHARS,
    MAX_CHARS,
    UsageError,
    error_result,
    result,
    truncate,
)
from .sessions import DEFAULT_IDLE_TIMEOUT, BrowserSession, Hosted, SessionRegistry

ACTION_LIMIT = 80
EVALUATE_MAX_CHARS = 30_000
MAX_STEPS = 20
STEPS_TIMEOUT = 45.0
TEXT_WAIT = 3.0
REF_CHECK_LIMIT = 10_000
ELEMENT_TIMEOUT = 10.0
MAX_ELEMENT_TIMEOUT = 30.0
NAV_TIMEOUT = 30.0
NAVIGATE_BUDGET = 50.0
REPLY_MARGIN = 8.0
MIN_CHALLENGE_TIMEOUT = 5.0
REFOCUS_WAIT = 0.5

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
ACTING = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)


SessionId = Annotated[
    str | None,
    Field(description="Session from surfsky_session_start; omit for the current one."),
]
Target = Annotated[
    str | None,
    Field(
        description="CSS selector, @N from a snapshot of this page, or text=words (the first visible "
        "element whose name contains the words). No XPath."
    ),
]
ProfileUuid = Annotated[
    str | None,
    Field(
        description="Saved profile to load: its cookies and logins come with it and are saved back on a "
        "normal stop. Only a uuid the user gave you. Not with os, os_version or arch."
    ),
]
Proxy = Annotated[
    str | None,
    Field(
        description="Omit for the account default. 'premium': residential pool; 'mobile': mobile pool; "
        "'shared': free pool, tests only; or your own proxy URL (user:pass@host:port), then without "
        "country, region and city."
    ),
]
Country = Annotated[
    str | None, Field(description="Proxy exit country code, for example US.")
]
Region = Annotated[
    str | None, Field(description="Proxy exit state or region, with country.")
]
City = Annotated[str | None, Field(description="Proxy exit city, with country.")]
OS = Annotated[
    Literal["win", "mac", "android"] | None,
    Field(description="Fingerprint operating system. Not with profile_uuid."),
]
OSVersion = Annotated[str | None, Field(description="Fingerprint OS version, with os.")]
Arch = Annotated[
    Literal["x86", "arm"] | None,
    Field(description="Fingerprint CPU architecture, with os."),
]
Blocked = Annotated[
    list[Literal["image", "font", "media", "stylesheet"]] | None,
    Field(
        description="Resource types not downloaded: faster and less proxy traffic, but screenshots and "
        "layout lose them."
    ),
]
WaitUntil = Annotated[
    Literal["domcontentloaded", "load", "networkidle", "commit"],
    Field(
        description="Load state that ends the navigation: 'load' (default); 'domcontentloaded' (faster on heavy "
        "pages; then surfsky_wait for what you need); 'commit' (first response); 'networkidle' (busy pages "
        "never reach it and time out)."
    ),
]
NavTimeout = Annotated[
    float, Field(gt=0, description="Seconds to wait for the navigation (default 30).")
]
MaxChars = Annotated[
    int,
    Field(
        ge=1,
        description=f"Cut each text after this many characters (default {DEFAULT_MAX_CHARS}, at most {MAX_CHARS}).",
    ),
]
OnlyMain = Annotated[
    bool,
    Field(
        description="Keep the main or article content; drop navigation, headers, footers, asides and forms."
    ),
]
IncludeSnapshot = Annotated[
    bool,
    Field(description="Include a snapshot of interactive elements (default true)."),
]

ToolFn = Callable[..., Awaitable[CallToolResult]]


def refuse_unknown(extra: dict[str, Any] | None) -> None:
    if extra:
        raise UsageError(f"unknown argument: {', '.join(extra)}")


TOOLS: list[tuple[str, ToolFn, str, ToolAnnotations, dict[str, Any] | None]] = []


def tool(
    title: str, annotations: ToolAnnotations, meta: dict[str, Any] | None = None
) -> Callable[[ToolFn], ToolFn]:
    """Register a tool whose every failure is an is_error result with a stable code."""

    def decorate(fn: ToolFn) -> ToolFn:
        @functools.wraps(fn)
        async def run(
            *args: Any, unknown: dict[str, Any] | None = None, **kwargs: Any
        ) -> CallToolResult:
            try:
                refuse_unknown(unknown)
                return await fn(*args, **kwargs)
            except Exception as exc:
                return error_result(exc)

        TOOLS.append((run.__name__, run, title, annotations, meta))
        return run

    return decorate


def slim(node: Any, defs: dict[str, Any]) -> Any:
    """A listed schema without what pydantic generates for itself: titles, defaults,
    `anyOf [X, null]` for an optional X, and `$defs` used once."""
    if isinstance(node, list):
        return [slim(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return slim(defs[node["$ref"].rpartition("/")[2]], defs)
    kept = {
        key: {name: slim(schema, defs) for name, schema in value.items()}
        if key == "properties"
        else slim(value, defs)
        for key, value in node.items()
        if key not in ("title", "default", "$defs")
    }
    options = [option for option in kept.get("anyOf", []) if option != {"type": "null"}]
    if len(options) == 1:
        del kept["anyOf"]
        kept = {**options[0], **kept}
    return kept


def register(server: MCPServer) -> None:
    for name, run, title, annotations, meta in TOOLS:
        server.tool(
            name=name,
            title=title,
            description=inspect.cleandoc(run.__doc__ or ""),
            annotations=annotations,
            meta=meta,
        )(run)
        listed = server._tool_manager.get_tool(name)
        assert listed is not None
        listed.parameters = slim(listed.parameters, listed.parameters.get("$defs", {}))

        class Arguments(listed.fn_metadata.arg_model):  # ty: ignore[unsupported-base]
            model_config = ConfigDict(extra="allow")

            def model_dump_one_level(self) -> dict[str, Any]:
                return {**super().model_dump_one_level(), "unknown": self.model_extra}

        listed.fn_metadata.arg_model = Arguments


def get_registry(ctx: Context) -> SessionRegistry:
    state = ctx.request_context.lifespan_context
    if isinstance(state, Hosted):
        access = get_access_token()
        assert access is not None  # the bearer middleware refused anything else
        return state.registry(access)
    return cast(SessionRegistry, state)


def acquire(
    ctx: Context, session_id: str | None
) -> AbstractAsyncContextManager[BrowserSession]:
    """The named session, else the connection's current one, else the default one."""
    registry = get_registry(ctx)
    return registry.acquire(session_id or registry.current_for(connection_id(ctx)))


def connection_id(ctx: Context) -> str | None:
    # absent on stdio and on protocol revisions without a handshake
    if connection := (ctx.headers or {}).get("mcp-session-id"):
        return connection
    access = get_access_token()
    return f"app:{access.subject}" if access and access.subject else None


def session_options(
    proxy: Proxy,
    country: Country,
    region: Region,
    city: City,
    os: OS,
    os_version: OSVersion,
    arch: Arch,
    block_resources: Blocked,
) -> dict[str, Any]:
    """Session identity options in the CLI's names: `os_`, comma-separated `block`."""
    opts = {
        "proxy": "premium" if proxy == "mobile" else proxy,
        "country": country,
        "region": region,
        "city": city,
        "proxy_type": "mobile" if proxy == "mobile" else None,
        "os_": os,
        "os_version": os_version,
        "arch": arch,
        "block": ",".join(block_resources or ()),
    }
    try:
        return cli.session_kwargs(opts)
    except click.UsageError as exc:
        raise UsageError(exc.format_message()) from exc


async def navigate(
    page: Any, nav: Awaitable[Any], wait_until: str, before: str
) -> str | None:
    """Run a navigation, tolerating a lifecycle event that never arrives."""
    try:
        await nav
    except BrowserTimeoutError:
        if await page.url() == before:
            raise
        return f"navigation did not reach '{wait_until}'; the page may be partly loaded"
    return None


async def challenge_note(page: Any, deadline: float) -> str | None:
    left = deadline - anyio.current_time()
    if left - REPLY_MARGIN < MIN_CHALLENGE_TIMEOUT:
        return "challenge_page not checked, no time left in this call"
    try:
        with anyio.fail_after(left):
            outcome = await page.pass_challenge(timeout=left - REPLY_MARGIN)
    except TimeoutError:
        return "challenge_page check did not answer in time; a solve may have been billed"
    except SurfskyError:
        return "challenge_page check failed; the page may be a challenge page, check its title"
    if outcome.status == "none":
        return None
    details = [outcome.reason if outcome.status == "not_solved" else None]
    if outcome.cost_usd:
        details.append(f"billed ${outcome.cost_usd:g}")
    details = ", ".join(filter(None, details))
    details = f" ({details})" if details else ""
    if outcome.status == "blocked":
        return f"block_page met{details}; start a new session with another fingerprint OS or proxy location, or use surfsky_scrape"
    note = f"challenge_page met, {outcome.status.replace('_', ' ')}{details}"
    if outcome.status == "not_solved":
        return f"{note}; surfsky_scrape may get this page"
    if outcome.reason == "load_timeout":
        note += "; the page may be partly loaded"
    return note


async def page_state(
    live: BrowserSession, **extra: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The header (observed) and page (page-controlled) halves of a session result."""
    header: dict[str, Any] = {
        "session_id": live.id,
        "url": await live.page.url(),
        **extra,
    }
    # on one line: a break would start a forged row in a snapshot
    page: dict[str, Any] = {"title": " ".join((await live.page.title()).split())}
    if live.note:
        header["note"], live.note = live.note, None
    if live.dialogs:
        header["dialogs"] = len(live.dialogs)
        page["dialogs"] = list(live.dialogs)
        live.dialogs.clear()
    return header, page


# --- account and session lifecycle -------------------------------------------------


@tool("Account status", READ_ONLY)
async def surfsky_status(ctx: Context) -> CallToolResult:
    """Check the Surfsky account this server uses: credentials, plan limits, proxy quota, and
    the sessions this server owns (id, seconds idle, idle timeout). Starts no browser. Use
    it when a tool returns an auth, quota or plan_full error, or before starting parallel
    sessions to see how many the plan allows.
    """
    reg = get_registry(ctx)
    access = get_access_token()
    settings = Settings()
    if access is not None and access.claims:
        settings = Settings(
            api_token=access.claims["api_token"], base_url=access.claims.get("base_url")
        )
    info: dict[str, Any] = {
        "version": __version__,
        "base_url": settings.url(),
        "token_source": "request" if access else settings.token()[1],
    }
    info.update(await cloud_status(settings))
    now = time.time()
    info["sessions"] = [
        {
            "session_id": live.id,
            "default": live.default,
            "idle_s": int(now - live.last_used),
            "idle_timeout": live.idle_timeout,
        }
        for live in reg.sessions.values()
    ]
    return result(info)


@tool("Start a session", ACTING)
async def surfsky_session_start(
    ctx: Context,
    profile_uuid: ProfileUuid = None,
    proxy: Proxy = None,
    country: Country = None,
    region: Region = None,
    city: City = None,
    os: OS = None,
    os_version: OSVersion = None,
    arch: Arch = None,
    block_resources: Blocked = None,
    idle_timeout: Annotated[
        int,
        Field(
            ge=1,
            le=3600,
            description="Seconds without a tool call before the server stops the session (default 300).",
        ),
    ] = DEFAULT_IDLE_TIMEOUT,
    dialogs: Annotated[
        Literal["dismiss", "accept"],
        Field(
            description="Answer to alert, confirm and prompt dialogs: 'dismiss' (default) or 'accept'. "
            "Answered dialogs are reported in the next result."
        ),
    ] = "dismiss",
) -> CallToolResult:
    """Start a cloud browser session and return its session_id. The hosted server needs it
    before any browser tool. A local server needs it only for a saved profile, a proxy
    location, a fingerprint OS, a second browser in parallel, or a session of your own
    when other agents share the server; otherwise the browser tools share a default
    session. The session bills per minute until surfsky_session_stop or idle_timeout
    seconds without a tool call: stop it when the task is done. It opens on about:blank;
    call surfsky_navigate next. The result's live_url lets the user watch the browser
    and act in it: give it only to the user.
    """
    if profile_uuid and (os or os_version or arch):
        raise UsageError(
            "a profile owns its fingerprint: drop os, os_version and arch, or drop profile_uuid"
        )
    if not 1 <= idle_timeout <= 3600:
        raise UsageError("idle_timeout is 1..3600 seconds")
    options = session_options(
        proxy, country, region, city, os, os_version, arch, block_resources
    )
    registry = get_registry(ctx)
    live = await registry.start(
        owner=connection_id(ctx) if registry.hosted else None,
        profile_uuid=profile_uuid,
        idle_timeout=idle_timeout,
        dialogs=dialogs,
        **options,
    )
    urls = cli.urls(live.session)  # built from the start reply, no further API call
    return result(
        {
            "session_id": live.id,
            "profile_uuid": profile_uuid,
            "idle_timeout": live.idle_timeout,
            "live_url": urls.pop("screencast", None),
            **urls,
            "note": f"bills per minute until surfsky_session_stop or {live.idle_timeout} s without a tool call",
        }
    )


@tool("Stop a session", DESTRUCTIVE_IDEMPOTENT)
async def surfsky_session_stop(
    ctx: Context,
    session_id: Annotated[
        str,
        Field(description="The session to stop: any session id on the account."),
    ],
) -> CallToolResult:
    """Stop a session and its billing. Use it when a task is done, or to clean up a leaked
    session found with surfsky_session_list. Safe to repeat. A saved profile's cookies and
    logins are stored on a normal stop. Stopping the default session is fine: the next
    browser tool call starts a fresh one.
    """
    try:
        await get_registry(ctx).stop(session_id)
    except Exception as exc:
        return error_result(
            exc,
            hint=f"session {session_id} may keep billing; retry, or check surfsky_session_list",
        )
    return result({"session_id": session_id, "stopped": True})


@tool("List sessions", READ_ONLY)
async def surfsky_session_list(ctx: Context) -> CallToolResult:
    """List the account's running sessions: session_id, profile_uuid, started_at,
    active_seconds, whether this server started it (owned) and whether it is this
    server's default. Use it to find leaked sessions to stop.
    """
    reg = get_registry(ctx)
    active = await reg.client().profiles.list_active()
    alive = {item.internal_uuid for item in active}
    for live in list(reg.sessions.values()):
        if live.id not in alive and not live.lock.locked():
            reg.remove(live)
    rows = [
        {
            "session_id": item.internal_uuid,
            "profile_uuid": None if item.one_time else item.profile_uuid,
            "started_at": item.started_at,
            "active_seconds": item.active_seconds,
            "owned": item.internal_uuid in reg.sessions,
            "default": item.internal_uuid in reg.sessions
            and reg.sessions[item.internal_uuid].default,
        }
        for item in active
    ]
    return result({"count": len(rows), "sessions": rows})


# --- navigate and read -------------------------------------------------------------


def save_refs(
    live: BrowserSession, url: str, data: dict[str, Any], *, merge: bool = False
) -> None:
    """Replace the refs with the rows'. `merge` keeps those of this tab and URL:
    `find` lists a part of the page, and the agent may hold a ref from a fuller
    snapshot. Not after an action, which may have moved elements between selectors."""
    state = live.state
    same_page = merge and (state.get("refs_target_id"), state.get("refs_url")) == (
        live.page.target_id,
        url,
    )
    refs = state.get("refs") if same_page else None
    names = state.get("ref_names") if same_page else None
    snapshot.save_refs(state, live.page.target_id, url, data)
    state["refs"] = {**(refs or {}), **state["refs"]}
    state["ref_names"] = {
        **(names or {}),
        **{
            str(item["ref"]): [item["role"], item.get("name", "")]
            for item in data["items"]
        },
    }


def snapshot_digest(url: str, data: dict[str, Any]) -> str:
    return hashlib.sha1(
        json.dumps([url, data["items"]], sort_keys=True).encode()
    ).hexdigest()


def snapshot_result(
    header: dict[str, Any],
    page: dict[str, Any],
    data: dict[str, Any],
    details: bool = False,
) -> CallToolResult:
    """The walker's rows as text only: a host feeds the model a result's structured
    content when it has one, and a row costs twice as much there.

    A ref targets a row, so its selector is left out, and its href too unless
    another row has the same role and name."""
    if missing := data["total"] - len(data["items"]):
        header["more"] = (
            f"{missing} more elements: surfsky_snapshot with find= or a higher limit lists them"
        )
    lines = [f"title: {page['title']}"]
    for dialog in page.get("dialogs") or []:
        verdict = "accepted" if dialog["accepted"] else "dismissed"
        lines.append(
            f"dialog: {dialog['kind']} {json.dumps(dialog['message'], ensure_ascii=False)} {verdict}"
        )
    same = Counter((item["role"], item.get("name")) for item in data["items"])
    for item in data["items"]:
        if not details and same[item["role"], item.get("name")] == 1:
            item = {key: value for key, value in item.items() if key != "href"}
        row = snapshot.render_items({"items": [item], "total": 1})
        if details:
            row += f" selector={snapshot.token(item['selector'])}"
        lines.append(row)
    if unreachable := data.get("unreachable"):
        lines.append(
            snapshot.render_items({"items": [], "total": 0, "unreachable": unreachable})
        )
    out = result(header, page, body="\n".join(lines))
    out.structured_content = None
    return out


async def action_result(
    live: BrowserSession,
    before: str,
    *,
    want_snapshot: bool,
    status: bool = False,
    action: bool = False,
    **extra: Any,
) -> CallToolResult:
    """Settle, then report where the page is; with a snapshot, refresh refs and `changed`."""
    data = (
        await snapshot.after_action(live.page, limit=ACTION_LIMIT)
        if want_snapshot
        else None
    )
    if data and (note := data.pop("note", None)):
        live.note = f"{live.note}; {note}" if live.note else note
    header, page = await page_state(live, **extra)
    header["navigated"] = header["url"] != before
    if status:
        header["status"] = live.page.status
    if data is not None:
        save_refs(live, header["url"], data)
        new = snapshot_digest(header["url"], data)
        changed = header["navigated"] or new != live.snapshot_hash
        live.snapshot_hash = new
        if action:
            header["changed"] = changed
            if not changed:
                header["hint"] = (
                    "nothing observable changed; take a fresh snapshot or wait before repeating the action"
                )
        return snapshot_result(header, page, data)
    return result(header, page)


@tool("Navigate", ACTING)
async def surfsky_navigate(
    ctx: Context,
    url: Annotated[
        str | None,
        Field(description="URL to open; https:// is added when missing. Give url or go."),
    ] = None,
    go: Annotated[
        Literal["back", "forward", "reload"] | None,
        Field(description="History step instead of a URL."),
    ] = None,
    session_id: SessionId = None,
    wait_until: WaitUntil = "load",
    timeout: NavTimeout = NAV_TIMEOUT,
    snapshot: IncludeSnapshot = True,
) -> CallToolResult:
    """Open a URL in the session's active tab (or go back, forward or reload) and return
    where it landed: final url, HTTP status, navigated, and a snapshot of the first 80
    visible interactive elements with @N refs for surfsky_act; surfsky_snapshot with
    find= or a higher limit lists the rest. Use it for a page that needs clicks,
    scrolling, a form or a login; to only read a page, surfsky_scrape is cheaper and
    starts no session. A bot challenge page is passed, captcha solves are billed
    where the account pays for solving, and note reports it; a captcha on an
    ordinary page is never solved here. If note says not solved or block_page, or the
    title and snapshot lack the expected content, the page is not the real one
    whatever the status.
    """
    if (url is None) == (go is None):
        raise UsageError("pass url, or go (back, forward, reload), not both")
    async with acquire(ctx, session_id) as live:
        page = live.page
        deadline = anyio.current_time() + NAVIGATE_BUDGET
        before = await page.url()
        if url is not None:
            nav = page.goto(with_scheme(url), wait_until=wait_until, timeout=timeout)
        elif go == "reload":
            nav = page.reload(wait_until=wait_until, timeout=timeout)
        else:
            step = page.go_back if go == "back" else page.go_forward
            if await step(timeout=timeout) is None:
                raise UsageError("no further history")
            nav = page.wait_for_load_state(wait_until, timeout=timeout)
        for note in (
            await navigate(page, nav, wait_until, before),
            await challenge_note(page, deadline),
        ):
            if note:
                live.note = f"{live.note}; {note}" if live.note else note
        return await action_result(live, before, want_snapshot=snapshot, status=True)


@tool("Snapshot elements", READ_ONLY)
async def surfsky_snapshot(
    ctx: Context,
    session_id: SessionId = None,
    interactive_only: Annotated[
        bool,
        Field(description="List only elements that can be acted on, without headings."),
    ] = False,
    find: Annotated[
        str | None,
        Field(
            description="Case-insensitive substring matched against each element's role, name, href and CSS "
            "selector; a|b matches either."
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            ge=1,
            description="Maximum rows (default 500); the result says how many more matched.",
        ),
    ] = 500,
    details: Annotated[
        bool,
        Field(description="Add every link's href and each row's CSS selector."),
    ] = False,
) -> CallToolResult:
    """List the visible interactive elements (links, buttons, inputs, selects) and headings
    of the active tab as '[@N] role "name"' rows, with value, checked, options and
    disabled where present, and href for links that share a name; @N is a target for
    surfsky_act. Refs belong to this tab and URL: after a navigation an old ref fails
    with stale_ref. find narrows the list without renumbering. Iframe and shadow-root
    contents are not listed; they appear as '[--] iframe ... (content not reachable)'.
    For the page text use surfsky_read.
    """
    async with acquire(ctx, session_id) as live:
        data = await snapshot.take(
            live.page, limit=limit, interactive=interactive_only, find=find
        )
        header, page = await page_state(live)
        save_refs(live, header["url"], data, merge=True)
        live.snapshot_hash = snapshot_digest(header["url"], data)
        return snapshot_result(header, page, data, details)


# an SVG element has no innerText
FIRST = "(s, n, text) => Array.from(document.querySelectorAll(s)).slice(0, n).map(el => text ? (el.innerText ?? el.textContent) : el.outerHTML)"


@tool("Read page", READ_ONLY)
async def surfsky_read(
    ctx: Context,
    session_id: SessionId = None,
    format: Annotated[
        Literal["markdown", "text", "html", "links"],
        Field(
            description="'markdown' (default), 'text' (rendered innerText; ignores only_main_content), 'html' "
            "(without scripts and styles) or 'links' (absolute, deduplicated, in document order)."
        ),
    ] = "markdown",
    selector: Target = None,
    limit: Annotated[
        int | None,
        Field(
            ge=1,
            le=100,
            description="With selector: read the first N matching elements (at most 100) as separate items.",
        ),
    ] = None,
    only_main_content: OnlyMain = False,
    max_chars: MaxChars = DEFAULT_MAX_CHARS,
) -> CallToolResult:
    """Read the active tab as markdown (default), plain text, cleaned html or a list of
    links. Use it after surfsky_navigate or surfsky_act to get the page's text; for a URL
    that needs no clicks or login, surfsky_scrape does the same without a session.
    selector restricts the output to one element; with limit, to the first N matches,
    for repeated items like result cards. Content longer than max_chars is cut at
    a line boundary with a marker: raise max_chars or pass selector for the rest.
    """
    if limit and not selector:
        raise UsageError("limit needs selector")
    async with acquire(ctx, session_id) as live:
        page = live.page
        css = await resolve_target(live, selector) if selector else None
        url = await page.url()
        if limit:
            found = await page.evaluate(FIRST, css, limit, format == "text")
        elif format == "text":
            found = [await page.inner_text(css or "body")]
        else:
            found = [await page.outer_html(css) if css else await page.content()]
        if not found or found[0] is None:
            raise NotFound(f"nothing matches {selector!r}")

        def rendered(part: str) -> str:
            if format == "text":
                return part
            soup = html.clean(part, main_content=only_main_content)
            if format == "markdown":
                return html.to_markdown(soup)
            return str(soup) if format == "html" else "\n".join(html.links(soup, url))

        extra: dict[str, Any] = {"format": format}
        if limit:
            extra["matches"] = total = await page.count(css)
            content = "\n\n".join(
                f"[{index} of {total}]\n{rendered(part).strip()}"
                for index, part in enumerate(found, 1)
            )
        else:
            content = rendered(found[0])
        shown, truncated = truncate(
            content, max_chars, hint=f"raise max_chars up to {MAX_CHARS} or pass selector"
        )
        header, page_data = await page_state(live, **extra)
        if truncated:
            header["truncated"] = True
            header["total_chars"] = len(content)
        page_data["content"] = shown
        return result(header, page_data, body=f"title: {page_data['title']}\n\n{shown}")


@tool("Screenshot", READ_ONLY)
async def surfsky_screenshot(
    ctx: Context,
    session_id: SessionId = None,
    full_page: Annotated[
        bool,
        Field(
            description="Capture the whole scrollable page as PNG instead of the viewport."
        ),
    ] = False,
    selector: Target = None,
) -> CallToolResult:
    """Capture the active tab as an image: the viewport as JPEG, or PNG for the full page
    or one element (selector). Take one only when the user asks for an image or the
    answer depends on how the page looks: a layout, a chart, or a captcha or canvas the
    text tools cannot read. Not to check that an action worked: its result already
    carries the page state. For what can be clicked use surfsky_snapshot; for content,
    surfsky_read.
    """
    async with acquire(ctx, session_id) as live:
        await live.page.bring_to_front()  # a hidden tab's capture hangs
        css = await resolve_target(live, selector) if selector else None
        fmt: Literal["png", "jpeg"] = "png" if (css or full_page) else "jpeg"
        try:
            data = await live.page.screenshot(
                selector=css,
                full_page=full_page,
                format=fmt,
                quality=60 if fmt == "jpeg" else None,
            )
        except ValueError as exc:
            raise NotFound(str(exc)) from exc
        header, page = await page_state(live, bytes=len(data), format=fmt)
        return result(header, page, image=data, image_format=fmt)


@tool("Evaluate JavaScript", ACTING)
async def surfsky_evaluate(
    ctx: Context,
    expression: Annotated[
        str,
        Field(
            description="A JavaScript expression, or a function (arrow or function keyword) that receives args."
        ),
    ],
    session_id: SessionId = None,
    args: Annotated[
        list[Any] | None,
        Field(description="JSON values passed to the function as arguments."),
    ] = None,
    main_world: Annotated[
        bool,
        Field(
            description="Run in the page's main world (its globals; the page can observe the call) instead of "
            "the isolated world."
        ),
    ] = False,
) -> CallToolResult:
    """Run JavaScript in the active tab and return its JSON result: an expression, or a
    function called with args; a returned promise is awaited, so an async function can
    poll for a condition. Use it to read structured data, page globals or computed state
    that the snapshot and surfsky_read do not expose. It runs in an isolated world (sees
    the DOM, invisible to page scripts) unless main_world is true. Do not click or set
    values here: that fires synthetic events bot detection flags and skips the site's
    handlers; input goes through surfsky_act. Results over 30000 characters are cut.
    """
    async with acquire(ctx, session_id) as live:
        value = await live.page.evaluate(
            expression, *(args or []), isolated=not main_world
        )
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        shown, truncated = truncate(
            text,
            EVALUATE_MAX_CHARS,
            hint="return less from the script: slice, map or filter",
        )
        header, page = await page_state(live)
        if truncated:
            header["truncated"] = True
        page["result"] = shown if truncated else value
        return result(header, page, body=shown)


GONE = "s => { const el = document.querySelector(s); return !el || !el.getClientRects().length; }"
HAS_TEXT = "t => (document.body ? document.body.innerText : '').includes(t)"


def require(value: str | None, action: str, name: str) -> str:
    if value is None:
        raise UsageError(f"{action} needs {name}")
    return value


async def resolve_target(
    live: BrowserSession, target: str, *, later: bool = False
) -> str:
    """`later` marks a step after the first: its text= target may still be rendering."""
    try:
        if later and target.startswith("text="):
            # what the previous step brought up (a dropdown, a dialog) may still be rendering
            with anyio.move_on_after(TEXT_WAIT):
                while True:
                    try:
                        return await cli.resolve_target(live, target)
                    except NotFound:
                        await anyio.sleep(0.3)
        css = await cli.resolve_target(live, target)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    if target.startswith("@"):
        # the selector still matches one element: make sure it is the one the agent saw
        seen = (live.state.get("ref_names") or {}).get(target[1:])
        fresh = await snapshot.take(live.page, limit=REF_CHECK_LIMIT)
        now = [
            [item["role"], item.get("name", "")]
            for item in fresh["items"]
            if item["selector"] == css
        ]
        if now != [seen]:
            raise StaleRef(f"{target} changed since the snapshot")
    return css


# The element typing goes to, kept in the isolated world for the check after it. One
# the page replaced or navigated away from meanwhile is not looked up again: another
# element under the same selector says nothing about where the keys went.
FIELD = r"""(selector, mark) => {
  const active = () => {
    let el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    return el;
  };
  const editable = el => el.isContentEditable || el.tagName === 'IFRAME'
    || el.matches('textarea, [role=textbox], [role=combobox], [role=searchbox], input:not([type=button],'
      + ' [type=submit], [type=reset], [type=checkbox], [type=radio], [type=file], [type=image],'
      + ' [type=range], [type=color])');
  if (mark) globalThis.__surfskyField = selector ? document.querySelector(selector) : active();
  const el = globalThis.__surfskyField;
  if (!el || !el.isConnected) return null;
  const value = el.matches('input, textarea') ? el.value : el.isContentEditable ? el.innerText : null;
  return {editable: editable(el), value, focused: active() === el};
}"""


async def enter(
    page: Any, css: str | None, text: str, target: str, replace: bool = False
) -> None:
    """Type into `css`, or into the focused element.

    Human.type goes to whatever has focus, and a page can move it: a dialog that
    opens under the click or between the click and the keys. Typing is repeated only
    when no key was sent; after that the page may have used the text (a chat box that
    clears itself, a code field that submits) and a second run would double it."""
    before = await page.evaluate(FIELD, css, True)
    if css is None:
        if not (before and before["editable"]):
            raise ValueError(
                "no editable element has focus; type with the field as target"
            )
        await page.keyboard.type(text)
    else:
        send = page.fill if replace else page.type
        try:
            await send(css, text)
        except ValueError as exc:
            if "did not take focus" not in str(exc):
                raise
            await anyio.sleep(REFOCUS_WAIT)  # the click did not focus it: no key was sent
            try:
                await send(css, text)
            except ValueError:
                # the SDK's message carries the selector, which the page controls
                raise ValueError(f"{target} did not take focus") from None
    after = None
    with suppress(SurfskyError):  # typing that navigates leaves no field to read
        after = await page.evaluate(FIELD, css, False)
    # anything else is the page's own handling of the text: a mask, a field that clears
    if (
        text
        and before
        and after
        and isinstance(after["value"], str)
        and after["value"] == before["value"]
        and not after["focused"]
        and " ".join(text.split()) not in " ".join(after["value"].split())
    ):
        raise ValueError(
            "the page moved focus away while typing and the text did not arrive: "
            "a dialog may have opened"
        )


Action = Literal[
    "click", "dblclick", "hover", "fill", "type", "press", "select", "scroll"
]
Modifiers = list[Literal["Alt", "Control", "Meta", "Shift"]]
NEEDS: dict[str, tuple[str, ...]] = {
    "click": ("target",),
    "dblclick": ("target",),
    "hover": ("target",),
    "fill": ("target", "text"),
    "type": ("text", "target"),
    "press": ("key",),
    "select": ("target", "value"),
    "scroll": (),
}


class Step(BaseModel):
    # unknown fields are refused by name in surfsky_act
    model_config = ConfigDict(
        extra="allow", json_schema_extra={"additionalProperties": False}
    )

    action: Action
    target: str | None = None
    text: str | None = None
    key: str | None = None
    value: str | None = None
    modifiers: Modifiers | None = None
    delta_y: float | None = None


async def perform(live: BrowserSession, step: Step, timeout: float, later: bool) -> None:
    page = live.page
    target = step.target or ""
    match step.action:
        case "click" | "dblclick" | "hover":
            css = await resolve_target(live, target, later=later)
            if step.action == "hover":
                await page.hover(css, timeout=timeout)
            elif step.action == "click":
                await page.click(css, modifiers=step.modifiers, timeout=timeout)
            else:
                await page.dblclick(css, modifiers=step.modifiers, timeout=timeout)
        case "fill":
            css = await resolve_target(live, target, later=later)
            await enter(page, css, step.text or "", target, replace=True)
        case "type":
            css = (
                None
                if target == cli.FOCUSED
                else await resolve_target(live, target, later=later)
            )
            await enter(page, css, step.text or "", target)
        case "press":
            await page.keyboard.press(step.key, modifiers=step.modifiers)
        case "select":
            css = await resolve_target(live, target, later=later)
            try:
                try:
                    await page.select_option(css, step.value)
                except ValueError:
                    await page.select_option(css, label=step.value)
            except ValueError:
                # the SDK's message carries the selector, which the page controls
                raise NotFound(f"{target} has no option {step.value!r}") from None
        case "scroll":
            if target:
                css = await resolve_target(live, target, later=later)
                await page.scroll_into_view(css)
            else:
                dy = step.delta_y
                if dy is None:  # a reader paging: most of a screen, no page JS
                    metrics = await page.send("Page.getLayoutMetrics")
                    dy = round(
                        metrics["cssVisualViewport"]["clientHeight"]
                        * random.uniform(0.6, 0.9)
                    )
                await page.scroll(delta_y=dy)


async def step_failure(
    live: BrowserSession,
    before: str,
    exc: Exception,
    index: int,
    steps: list[Step],
    hint: str | None = None,
) -> CallToolResult:
    """The failed step's error with the page as the steps before it left it."""
    failed = error_result(exc, hint=hint)
    error = (failed.structured_content or {})["error"]
    error["step"] = index + 1
    done = (
        "no step is done"
        if index == 0
        else "step 1 is done"
        if index == 1
        else f"steps 1-{index} are done"
    )
    head = cast(TextContent, failed.content[0]).text.replace(
        f"error [{error['code']}]:",
        f"error [{error['code']}] at step {index + 1} of {len(steps)} ({steps[index].action}):",
        1,
    )
    failed.content = [TextContent(type="text", text=head)]
    try:
        state = await action_result(live, before, want_snapshot=True)
    except Exception:
        return failed
    text = f"{head}\n{done}; the page is now:\n{cast(TextContent, state.content[0]).text}"
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"error": error},
        is_error=True,
    )


@tool("Act on the page", ACTING)
async def surfsky_act(
    ctx: Context,
    action: Annotated[
        Action | None,
        Field(description="The action to perform; or pass steps."),
    ] = None,
    session_id: SessionId = None,
    target: Target = None,
    text: Annotated[str | None, Field(description="Text for fill or type.")] = None,
    key: Annotated[
        str | None,
        Field(
            description="Key for press: Enter, Tab, Escape, Backspace, ArrowDown, or a combination like Control+A."
        ),
    ] = None,
    value: Annotated[
        str | None,
        Field(description="Option for select: its value attribute or its visible label."),
    ] = None,
    modifiers: Annotated[
        Modifiers | None,
        Field(description="Modifier keys held during click, dblclick or press."),
    ] = None,
    delta_y: Annotated[
        float | None,
        Field(description="Pixels to scroll the page: positive down, negative up."),
    ] = None,
    steps: Annotated[
        list[Step] | None,
        Field(
            description=f"Up to {MAX_STEPS} actions run in order, instead of action: objects with the same "
            "fields (action, target, text, key, value, modifiers, delta_y). After a step that "
            "changes the page, target by CSS selector or text=words: @N refs are from the snapshot before "
            "the call."
        ),
    ] = None,
    timeout: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_ELEMENT_TIMEOUT,
            description="Seconds allowed per action, including finding the element (default 10, at most 30).",
        ),
    ] = ELEMENT_TIMEOUT,
    snapshot: IncludeSnapshot = True,
) -> CallToolResult:
    """Perform input on the active tab with human-like mouse and keyboard, then return the
    page state (url, navigated, changed) and a fresh snapshot with @N refs. Use it for
    every click, form entry and keypress; pass steps to run a known sequence (fill a
    form, then submit) in one call. Actions: click, dblclick, hover (target); fill
    (target, text: replaces the value); type (target, text: appends keystrokes; target
    '@focused' types into the focused element without clicking); press (key); select
    (target, value); scroll (target scrolls it into view, delta_y scrolls the page,
    neither scrolls most of a screen down). steps stop at the first failure: the error
    names the step and carries the page state and snapshot to continue from. changed:
    false means nothing observable happened: snapshot or wait instead of repeating.
    Stop before a payment or final confirmation the user did not ask for.
    """
    if (action is None) == (steps is None):
        raise UsageError("pass action, or steps, not both")
    if steps is not None and not 1 <= len(steps) <= MAX_STEPS:
        raise UsageError(f"steps takes 1 to {MAX_STEPS} actions")
    todo = steps or [
        Step(
            action=cast(Action, action),
            target=target,
            text=text,
            key=key,
            value=value,
            modifiers=modifiers,
            delta_y=delta_y,
        )
    ]
    for step in todo:
        refuse_unknown(step.model_extra)
        for name in NEEDS[step.action]:
            require(getattr(step, name), step.action, name)
    async with acquire(ctx, session_id) as live:
        before = await live.page.url()
        deadline = anyio.current_time() + STEPS_TIMEOUT
        for index, step in enumerate(todo):
            try:
                with anyio.move_on_after(deadline - anyio.current_time()) as call:
                    if index:
                        await wait_for_load(live.page)
                    with anyio.fail_after(timeout):
                        await perform(live, step, timeout, later=index > 0)
            except Exception as exc:
                if steps is None:
                    raise
                return await step_failure(live, before, exc, index, todo)
            if call.cancelled_caught:
                late = TimeoutError(
                    f"the call reached {STEPS_TIMEOUT:.0f} s, the limit of one call, "
                    "before this step finished"
                )
                hint = "see what the page shows below, then send what is left in another call"
                return await step_failure(live, before, late, index, todo, hint)
        extra = {"steps": len(todo)} if steps else {}
        return await action_result(
            live, before, want_snapshot=snapshot, action=True, **extra
        )


@tool("Wait for the page", READ_ONLY)
async def surfsky_wait(
    ctx: Context,
    session_id: SessionId = None,
    selector: Annotated[
        str | None,
        Field(
            description="Element to wait for: a CSS selector (also for elements that do not exist yet), or @N "
            "or text=words, which must resolve now."
        ),
    ] = None,
    gone: Annotated[
        bool,
        Field(description="With selector: wait until it is absent or has no layout box."),
    ] = False,
    url_contains: Annotated[
        str | None, Field(description="Substring the page URL must contain.")
    ] = None,
    text: Annotated[
        str | None, Field(description="String the page's visible text must contain.")
    ] = None,
    seconds: Annotated[
        float | None,
        Field(gt=0, description="Fixed sleep in seconds, after the other conditions."),
    ] = None,
    timeout: Annotated[
        float, Field(gt=0, description="Seconds allowed per condition (default 30).")
    ] = NAV_TIMEOUT,
) -> CallToolResult:
    """Wait until the active tab meets a condition, then return its url and title. Use it
    for content that loads after an action or after a domcontentloaded navigation;
    actions settle on their own, so do not wait after every step. Several conditions
    are waited one after another, each with the full timeout; one not met in time fails
    with a timeout error and leaves the page as it is. For a condition only script can
    check, use surfsky_evaluate with an async function that polls.
    """
    if not any([selector, url_contains, text, seconds]):
        raise UsageError("pass selector, url_contains, text or seconds")
    async with acquire(ctx, session_id) as live:
        page = live.page
        if selector:
            css = await resolve_target(live, selector)
            if gone:
                await page.wait_for_function(GONE, css, timeout=timeout)
            else:
                await page.wait_for_selector(css, timeout=timeout)
        if url_contains:
            await page.wait_for_url(url_contains, timeout=timeout)
        if text:
            await page.wait_for_function(HAS_TEXT, text, timeout=timeout)
        if seconds:
            await anyio.sleep(seconds)
        header, page_data = await page_state(live)
        return result(header, page_data)


def find_tab(live: BrowserSession, which: str) -> Any:
    """A number from the last `list`, or a target id prefix from the live tabs."""
    pages = live.browser.pages
    if which.isdigit() and len(which) <= 2:
        order = live.state.get("tabs") or [page.target_id for page in pages]
        index = int(which)
        if index < len(order):
            for page in pages:
                if page.target_id == order[index]:
                    return page
        raise UsageError(f"no tab {which}; call surfsky_tab list")
    for page in pages:
        if page.target_id.startswith(which):
            return page
    raise UsageError(f"no tab {which!r}; call surfsky_tab list")


@tool("Manage tabs", ACTING)
async def surfsky_tab(
    ctx: Context,
    action: Annotated[
        Literal["list", "new", "switch", "close"],
        Field(
            description="list; new (optionally at url); switch (tab); close (tab, default the active one)."
        ),
    ],
    session_id: SessionId = None,
    tab: Annotated[
        str | None,
        Field(
            description="Tab for switch or close: its index from the last list, or its id (a prefix is enough)."
        ),
    ] = None,
    url: Annotated[
        str | None,
        Field(description="For new: URL to open in the new tab."),
    ] = None,
) -> CallToolResult:
    """Manage the tabs of a session: list (index, id, url, title, active), new (opens a tab
    and makes it active), switch, close. Use list then switch when a click opened a popup
    or a target=_blank link: the new page is another tab. Refs belong to a tab: take
    surfsky_snapshot after switching. The session's first tab cannot be closed; navigate
    it instead. The cloud allows five tabs per session.
    """
    async with acquire(ctx, session_id) as live:
        browser = live.browser
        if action == "list":
            rows = []
            for index, page in enumerate(browser.pages):
                rows.append(
                    {
                        "index": index,
                        "id": page.target_id[:8],
                        "active": page is live.page,
                        "url": await page.url(),
                        "title": await page.title(),
                    }
                )
            live.state["tabs"] = [page.target_id for page in browser.pages]
            return result(
                {"session_id": live.id, "count": len(rows)},
                {"tabs": rows},
                body=render(rows),
            )
        if action == "new":
            page = await browser.new_page()
            if url:
                await page.goto(with_scheme(url))
            live.page, live.state["target_id"] = page, page.target_id
            header, page_data = await page_state(live, tab=page.target_id[:8])
            return result(header, page_data)
        if action == "switch":
            page = find_tab(live, require(tab, action, "tab"))
            await page.bring_to_front()  # a hidden tab's screenshot hangs
            live.page, live.state["target_id"] = page, page.target_id
            header, page_data = await page_state(live, tab=page.target_id[:8])
            return result(header, page_data)
        page = find_tab(live, tab) if tab else live.page
        if page.target_id == browser.target_id:
            raise UsageError(
                "the session's first tab stays open; navigate it, or close the other tabs"
            )
        await page.close()
        if live.page is page:
            live.page, live.state["target_id"] = browser, None
        return result({"session_id": live.id, "closed": page.target_id[:8]})


# the one-call path is worth its definition in every conversation; the rest load on demand
@tool("Scrape a URL", READ_ONLY, meta={"anthropic/alwaysLoad": True})
async def surfsky_scrape(
    ctx: Context,
    url: Annotated[
        str,
        Field(description="URL to fetch; https:// is added when missing."),
    ],
    formats: Annotated[
        list[Literal["markdown", "html", "links", "screenshot"]] | None,
        Field(
            description="Any of markdown, html, links, screenshot. Default: markdown; add screenshot only "
            "when the user asks for an image (viewport JPEG)."
        ),
    ] = None,
    only_main_content: OnlyMain = False,
    wait_for: Annotated[
        str | None,
        Field(
            description="CSS selector to wait for before reading, for content that appears after load."
        ),
    ] = None,
    wait_until: WaitUntil = "load",
    timeout: NavTimeout = NAV_TIMEOUT,
    max_chars: MaxChars = DEFAULT_MAX_CHARS,
    profile_uuid: ProfileUuid = None,
    proxy: Proxy = None,
    country: Country = None,
    region: Region = None,
    city: City = None,
    os: OS = None,
    block_resources: Blocked = None,
) -> CallToolResult:
    """Fetch one URL and return it as markdown (default), cleaned html or links, with the
    final url, HTTP status and title. Use it whenever you
    only need to read a page: the Scraping API serves it from a pre-started browser
    behind a residential proxy and passes challenge pages and captchas, with no session
    to stop. profile_uuid, proxy, country, region, city or os start a
    throwaway browser for this call instead: slower, so pass them only when needed. For a
    page that needs clicks, a form or an interactive login, use surfsky_navigate and
    surfsky_act. A 200 with a short page may be a challenge page: read the content
    before trusting it. Text longer than max_chars is cut with a marker. Each call is
    billed.
    """
    if profile_uuid and os:
        raise UsageError("a profile owns its fingerprint: drop os, or drop profile_uuid")
    wanted = list(formats or ["markdown"])
    options = session_options(
        proxy, country, region, city, os, None, None, block_resources
    )
    client = get_registry(ctx).client()
    text_formats = [name for name in wanted if name != "screenshot"]
    if not (profile_uuid or options.keys() - {"block_resources"}):
        scraped = await client.scrape(
            with_scheme(url),
            formats=cast(Any, wanted),
            only_main_content=only_main_content,
            screenshot_options=ScreenshotOptions(
                full_page=False, format="jpeg", quality=60
            ),
            wait_until=wait_until,
            wait_for=wait_for,
            timeout=int(timeout * 1000),
            block_resources=block_resources,
        )
        built: dict[str, Any] = {
            "url": scraped.final_url or scraped.url,
            "status": scraped.status,
            "title": scraped.title,
            "markdown": scraped.markdown,
            "html": scraped.content,
            "links": scraped.links,
        }
        built = {name: value for name, value in built.items() if value is not None}
        image = base64.b64decode(scraped.screenshot) if scraped.screenshot else None
        challenge = scraped.challenge
        note = challenge and (
            f"{challenge.kind} met, {'solved' if challenge.solved else 'not solved'}"
        )
    else:
        # The SDK stops the session on exit and shields that stop from cancellation.
        async with client.browser(profile_uuid=profile_uuid, **options) as browser:
            before = await browser.url()
            nav = browser.goto(with_scheme(url), wait_until=wait_until, timeout=timeout)
            note = await navigate(browser, nav, wait_until, before)
            if wait_for:
                await browser.wait_for_selector(wait_for, timeout=timeout)
            data: dict[str, Any] = {
                "url": await browser.url(),
                "status": browser.status,
                "title": await browser.title(),
                "raw_html": await browser.content(),
            }
            image = (
                await browser.screenshot(format="jpeg", quality=60)
                if "screenshot" in wanted
                else None
            )
        built = build(data, text_formats, main_content=only_main_content)
    header: dict[str, Any] = {"url": built.get("url"), "status": built.get("status")}
    header["note"] = note
    page: dict[str, Any] = {"title": built.get("title")}
    parts: list[str] = []
    cut: list[str] = []
    for name in ("markdown", "html", "links"):
        if name not in built:
            continue
        content = "\n".join(built[name]) if name == "links" else built[name]
        shown, truncated = truncate(content, max_chars)
        page[name] = shown
        if truncated:
            cut.append(name)
        parts.append(shown if len(text_formats) == 1 else f"## {name}\n{shown}")
    if cut:
        header["truncated"] = cut
    body = f"title: {page['title']}\n\n" + "\n\n".join(parts)
    return result(header, page, body=body, image=image, image_format="jpeg")
