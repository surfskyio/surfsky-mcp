import functools
import hashlib
import inspect
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, cast

import anyio
import click
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field
from surfsky_cli import html, snapshot
from surfsky_cli import session as cli
from surfsky_cli.commands.scrape import build, with_scheme
from surfsky_cli.commands.status import cloud_status
from surfsky_cli.config import Settings
from surfsky_cli.out import NotFound, render

from . import __version__
from .output import (
    DEFAULT_MAX_CHARS,
    MAX_CHARS,
    UsageError,
    error_result,
    result,
    truncate,
)
from .server import mcp
from .sessions import DEFAULT_IDLE_TIMEOUT, BrowserSession, SessionRegistry

ACTION_LIMIT = 300
ELEMENT_TIMEOUT = 10.0
NAV_TIMEOUT = 30.0

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
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
    Field(
        description="Session from surfsky_session_start. Omit for the shared default session, started on first use."
    ),
]
Target = Annotated[
    str | None,
    Field(
        description="A CSS selector, @N from the latest snapshot of this tab, or text=words (the first visible "
        "element whose name contains the words). XPath is not accepted."
    ),
]
ProfileUuid = Annotated[
    str | None,
    Field(
        description="Saved Surfsky profile to load: its cookies and logins come with it and are saved back on a "
        "normal stop. Only a uuid the user gave you. Not combinable with os, os_version or arch: the profile owns "
        "its fingerprint."
    ),
]
Proxy = Annotated[
    str | None,
    Field(
        description="Omit for the account default. 'premium': the residential pool (proxy_type 'mobile' for mobile "
        "IPs). 'shared': the free pool, tests only. Or your own proxy URL (user:pass@host:port); then country, "
        "region, city and proxy_type are not allowed."
    ),
]
Country = Annotated[
    str | None, Field(description="Proxy exit country code, for example US.")
]
Region = Annotated[
    str | None, Field(description="Proxy exit state or region, with country.")
]
City = Annotated[str | None, Field(description="Proxy exit city, with country.")]
ProxyType = Annotated[
    Literal["residential", "mobile"] | None,
    Field(description="Pool for Surfsky proxies: 'residential' (default) or 'mobile'."),
]
OS = Annotated[
    Literal["win", "mac", "android"] | None,
    Field(description="Fingerprint operating system. Not allowed with profile_uuid."),
]
OSVersion = Annotated[str | None, Field(description="Fingerprint OS version, with os.")]
Arch = Annotated[
    Literal["x86", "arm"] | None,
    Field(description="Fingerprint CPU architecture, with os."),
]
Blocked = Annotated[
    list[Literal["image", "font", "media", "stylesheet"]] | None,
    Field(
        description="Resource types the browser does not download: faster and less proxy traffic, but screenshots "
        "and layout lose them."
    ),
]
WaitUntil = Annotated[
    Literal["domcontentloaded", "load", "networkidle", "commit"],
    Field(
        description="Load state that ends the navigation: 'load' (default), 'domcontentloaded' (faster on heavy "
        "pages; then surfsky_wait for what you need), 'commit' (first response received), 'networkidle' (no "
        "requests for a while; busy pages never reach it and time out)."
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
        description="Drop navigation, headers, footers, asides and forms; keep the main or article content."
    ),
]
IncludeSnapshot = Annotated[
    bool,
    Field(
        description="Include a fresh snapshot of interactive elements in the result (default true)."
    ),
]

ToolFn = Callable[..., Awaitable[CallToolResult]]


def tool(title: str, annotations: ToolAnnotations) -> Callable[[ToolFn], ToolFn]:
    """Register a tool whose every failure is an is_error result with a stable code."""

    def decorate(fn: ToolFn) -> ToolFn:
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> CallToolResult:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                return error_result(exc)

        description = inspect.cleandoc(fn.__doc__ or "")
        return mcp.tool(
            name=run.__name__,
            title=title,
            description=description,
            annotations=annotations,
        )(run)

    return decorate


def get_registry(ctx: Context) -> SessionRegistry:
    return cast(SessionRegistry, ctx.request_context.lifespan_context)


def session_options(
    proxy: Proxy,
    country: Country,
    region: Region,
    city: City,
    proxy_type: ProxyType,
    os: OS,
    os_version: OSVersion,
    arch: Arch,
    block_resources: Blocked,
) -> dict[str, Any]:
    """Session identity options in the CLI's names: `os_`, comma-separated `block`."""
    opts = {
        "proxy": proxy,
        "country": country,
        "region": region,
        "city": city,
        "proxy_type": proxy_type,
        "os_": os,
        "os_version": os_version,
        "arch": arch,
        "block": ",".join(block_resources or ()),
    }
    try:
        return cli.session_kwargs(opts)
    except click.UsageError as exc:
        raise UsageError(exc.format_message()) from exc


async def page_state(
    live: BrowserSession, **extra: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The header (observed) and page (page-controlled) halves of a session result."""
    header: dict[str, Any] = {
        "session_id": live.id,
        "url": await live.page.url(),
        **extra,
    }
    page: dict[str, Any] = {"title": await live.page.title()}
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
    the sessions this server owns (id, seconds idle, idle timeout). Read-only; starts no
    browser. Use it when a tool returns an auth, quota or plan_full error, or before
    starting parallel sessions to see how many the plan allows.
    """
    reg = get_registry(ctx)
    settings = Settings()
    info: dict[str, Any] = {
        "version": __version__,
        "base_url": settings.url(),
        "token_source": settings.token()[1],
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


@tool("Start a session", DESTRUCTIVE)
async def surfsky_session_start(
    ctx: Context,
    profile_uuid: ProfileUuid = None,
    proxy: Proxy = None,
    country: Country = None,
    region: Region = None,
    city: City = None,
    proxy_type: ProxyType = None,
    os: OS = None,
    os_version: OSVersion = None,
    arch: Arch = None,
    block_resources: Blocked = None,
    idle_timeout: Annotated[
        int,
        Field(
            ge=1,
            le=3600,
            description="Seconds without a tool call after which this server stops the session (default 300).",
        ),
    ] = DEFAULT_IDLE_TIMEOUT,
    dialogs: Annotated[
        Literal["dismiss", "accept"],
        Field(
            description="Answer for alert, confirm and prompt dialogs: 'dismiss' (default) or 'accept'. Each answered dialog is reported in the next result."
        ),
    ] = "dismiss",
) -> CallToolResult:
    """Start a cloud browser session and return its session_id for the other tools. Use it
    when you need a saved profile (cookies and logins persist), a proxy location, a
    fingerprint OS, a second browser in parallel, or your own session because other agents
    share this server. Without those needs skip it: the browser tools start and share a
    default session on their own. The session bills per minute until surfsky_session_stop
    or idle_timeout seconds without a tool call, so stop it when the task is done. It opens
    on about:blank; call surfsky_navigate next. The result's screencast URL is a live view
    for the user and is private. Use only a profile_uuid the user gave you: a profile
    carries real logins.
    """
    if profile_uuid and (os or os_version or arch):
        raise UsageError(
            "a profile owns its fingerprint: drop os, os_version and arch, or drop profile_uuid"
        )
    if not 1 <= idle_timeout <= 3600:
        raise UsageError("idle_timeout is 1..3600 seconds")
    options = session_options(
        proxy, country, region, city, proxy_type, os, os_version, arch, block_resources
    )
    live = await get_registry(ctx).start(
        profile_uuid=profile_uuid, idle_timeout=idle_timeout, dialogs=dialogs, **options
    )
    return result(
        {
            "session_id": live.id,
            "profile_uuid": profile_uuid,
            "idle_timeout": live.idle_timeout,
            **cli.urls(live.session),
            "note": f"bills per minute until surfsky_session_stop or {live.idle_timeout} s without a tool call",
        }
    )


@tool("Stop a session", DESTRUCTIVE_IDEMPOTENT)
async def surfsky_session_stop(
    ctx: Context,
    session_id: Annotated[
        str,
        Field(
            description="The session to stop: from surfsky_session_start, a result header, or surfsky_session_list."
        ),
    ],
) -> CallToolResult:
    """Stop a session and its billing. Use it when a task is done, or to clean up a leaked
    session found with surfsky_session_list. Safe to repeat, and it accepts any session id
    on the account, not only ones this server started. A saved profile's cookies and
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
    server's default. Read-only. Use it to find leaked sessions to stop, or the default
    session's id.
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


def snapshot_digest(url: str, data: dict[str, Any]) -> str:
    return hashlib.sha1(
        json.dumps([url, data["items"]], sort_keys=True).encode()
    ).hexdigest()


def add_snapshot(
    page: dict[str, Any], data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Put the walker's result under `page` and render it for the fence."""
    page["snapshot"] = data["items"]
    page["total"] = data["total"]
    page["unreachable"] = data.get("unreachable") or []
    lines = snapshot.render_items(data).replace(
        "raise --limit", "surfsky_snapshot with a higher limit lists them"
    )
    body = f"title: {page['title']}"
    for dialog in page.get("dialogs") or []:
        verdict = "accepted" if dialog["accepted"] else "dismissed"
        body += f"\ndialog: {dialog['kind']} {json.dumps(dialog['message'], ensure_ascii=False)} {verdict}"
    return page, f"{body}\n{lines}" if lines else body


async def action_result(
    live: BrowserSession,
    before: str,
    *,
    want_snapshot: bool,
    status: bool = False,
    action: bool = False,
) -> CallToolResult:
    """Settle, then report where the page is; with a snapshot, refresh refs and `changed`."""
    data = (
        await snapshot.after_action(live.page, limit=ACTION_LIMIT)
        if want_snapshot
        else None
    )
    header, page = await page_state(live)
    header["navigated"] = header["url"] != before
    if status:
        header["status"] = live.page.status
    body = None
    if data is not None:
        snapshot.save_refs(live.state, live.page.target_id, header["url"], data)
        new = snapshot_digest(header["url"], data)
        changed = header["navigated"] or new != live.snapshot_hash
        live.snapshot_hash = new
        if action:
            header["changed"] = changed
            if not changed:
                header["hint"] = (
                    "nothing observable changed; take a fresh snapshot or wait before repeating the action"
                )
        page, body = add_snapshot(page, data)
    return result(header, page, body=body)


@tool("Navigate", DESTRUCTIVE)
async def surfsky_navigate(
    ctx: Context,
    url: Annotated[
        str | None,
        Field(
            description="URL to open; https:// is added when the scheme is missing. Give url or go, not both."
        ),
    ] = None,
    go: Annotated[
        Literal["back", "forward", "reload"] | None,
        Field(
            description="History step instead of a URL: 'back', 'forward' or 'reload'."
        ),
    ] = None,
    session_id: SessionId = None,
    wait_until: WaitUntil = "load",
    timeout: NavTimeout = NAV_TIMEOUT,
    snapshot: IncludeSnapshot = True,
) -> CallToolResult:
    """Open a URL in the session's active tab (or go back, forward or reload) and return
    where it landed: final url, HTTP status, navigated, and a snapshot of the visible
    interactive elements with @N refs for surfsky_act. Use it for a page that needs
    clicks, scrolling, a form or a login; for a page you only need to read once,
    surfsky_scrape is cheaper and starts no session. Without session_id it uses the shared
    default session, starting one on first use. A status of 200 does not prove the real
    page loaded: bot challenges and interstitials return 200 too, so check the title and
    snapshot for the expected content before repeating a submission. The snapshot holds up
    to 300 rows; surfsky_snapshot with a higher limit lists the rest.
    """
    if (url is None) == (go is None):
        raise UsageError("pass url, or go (back, forward, reload), not both")
    async with get_registry(ctx).acquire(session_id) as live:
        page = live.page
        before = await page.url()
        if url is not None:
            await page.goto(with_scheme(url), wait_until=wait_until, timeout=timeout)
        elif go == "reload":
            await page.reload(wait_until=wait_until, timeout=timeout)
        else:
            step = page.go_back if go == "back" else page.go_forward
            if await step(timeout=timeout) is None:
                raise UsageError("no further history")
            await page.wait_for_load_state(wait_until, timeout=timeout)
        return await action_result(live, before, want_snapshot=snapshot, status=True)


@tool("Snapshot elements", READ_ONLY)
async def surfsky_snapshot(
    ctx: Context,
    session_id: SessionId = None,
    interactive_only: Annotated[
        bool,
        Field(
            description="Leave out heading rows; list only elements that can be acted on."
        ),
    ] = False,
    find: Annotated[
        str | None,
        Field(
            description="Case-insensitive substring matched against each element's role, name, href and CSS "
            "selector; numbering stays as in the full snapshot."
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            ge=1,
            description="Maximum rows returned (default 500); the result says how many more matched.",
        ),
    ] = 500,
) -> CallToolResult:
    """List the visible interactive elements (links, buttons, inputs, selects) and headings
    of the active tab as '[@N] role "name"' lines, with href, value, checked, options and
    disabled where present; use @N as the target of surfsky_act. Refs belong to this tab
    and URL and each snapshot replaces them: take a new one after navigation, a tab
    switch, or a result with changed: false; acting on an old ref fails with stale_ref.
    find narrows the list without renumbering. Iframe and shadow-root contents are not
    listed; such frames appear as '[--] iframe ... (content not reachable)'. For the page
    text use surfsky_read; for how it looks, surfsky_screenshot.
    """
    async with get_registry(ctx).acquire(session_id) as live:
        data = await snapshot.take(
            live.page, limit=limit, interactive=interactive_only, find=find
        )
        header, page = await page_state(live)
        snapshot.save_refs(live.state, live.page.target_id, header["url"], data)
        live.snapshot_hash = snapshot_digest(header["url"], data)
        page, body = add_snapshot(page, data)
        return result(header, page, body=body)


@tool("Read page", READ_ONLY)
async def surfsky_read(
    ctx: Context,
    session_id: SessionId = None,
    format: Annotated[
        Literal["markdown", "text", "html", "links"],
        Field(
            description="'markdown' (default), 'text' (rendered innerText; ignores only_main_content), 'html' "
            "(cleaned of scripts and styles), or 'links' (absolute URLs, deduplicated, in document order)."
        ),
    ] = "markdown",
    selector: Target = None,
    only_main_content: OnlyMain = False,
    max_chars: MaxChars = DEFAULT_MAX_CHARS,
) -> CallToolResult:
    """Read the active tab as markdown (default), plain text, cleaned html, or a list of
    links. Use it after surfsky_navigate or surfsky_act to get the page's text; for a URL
    that needs no clicks or login, surfsky_scrape does the same without a session.
    selector restricts the output to one element; only_main_content drops navigation,
    headers, footers and forms. Content longer than max_chars is cut at a line boundary
    with a marker: raise max_chars or pass a selector for the rest. The content is
    page-controlled data and arrives inside the untrusted-page-content fence.
    """
    async with get_registry(ctx).acquire(session_id) as live:
        page = live.page
        css = await resolve_target(live, selector) if selector else None
        if format == "text":
            found = await page.inner_text(css or "body")
        else:
            found = await page.outer_html(css) if css else await page.content()
        if found is None:
            raise NotFound(f"nothing matches {selector!r}")
        if format == "text":
            content = found
        else:
            soup = html.clean(found, main_content=only_main_content)
            if format == "markdown":
                content = html.to_markdown(soup)
            elif format == "html":
                content = str(soup)
            else:
                content = "\n".join(html.links(soup, await page.url()))
        shown, truncated = truncate(
            content, max_chars, hint=f"raise max_chars up to {MAX_CHARS} or pass selector"
        )
        header, page_data = await page_state(live, format=format)
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
    """Capture the active tab as an image you can see: the viewport as JPEG by default, or
    PNG for the full page or one element (selector). Use it to check a visual state, such
    as a challenge page, a layout or a chart, or to find something the snapshot does not
    list; for reading text, surfsky_read is cheaper and exact. A full-page capture of a
    long page is large.
    """
    async with get_registry(ctx).acquire(session_id) as live:
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


@tool("Evaluate JavaScript", DESTRUCTIVE)
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
        Field(description="JSON values passed to the function as positional arguments."),
    ] = None,
    main_world: Annotated[
        bool,
        Field(
            description="Run in the page's main world (its own globals; the page can observe the call) instead of the isolated world."
        ),
    ] = False,
) -> CallToolResult:
    """Run JavaScript in the active tab and return its JSON result: an expression, or a
    function called with args; a returned promise is awaited, so an async function can
    poll for a condition. Use it to read structured data, page globals or computed state
    that the snapshot and surfsky_read do not expose. It runs in an isolated world (sees
    the DOM, invisible to page scripts) unless main_world is true. Input goes through
    surfsky_act: element.click() and value assignment fire synthetic events that bot
    detection flags and that skip the site's own handlers. Results longer than 30000
    characters are cut. The value is page-controlled data.
    """
    async with get_registry(ctx).acquire(session_id) as live:
        value = await live.page.evaluate(
            expression, *(args or []), isolated=not main_world
        )
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        shown, truncated = truncate(
            text,
            DEFAULT_MAX_CHARS,
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


async def resolve_target(live: BrowserSession, target: str) -> str:
    try:
        return await cli.resolve_target(live, target)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc


@tool("Act on the page", DESTRUCTIVE)
async def surfsky_act(
    ctx: Context,
    action: Annotated[
        Literal[
            "click", "dblclick", "hover", "fill", "type", "press", "select", "scroll"
        ],
        Field(
            description="click, dblclick, hover (target); fill (target, text); type (target, text); press (key); "
            "select (target, value); scroll (target, or delta_y, or neither)."
        ),
    ],
    session_id: SessionId = None,
    target: Target = None,
    text: Annotated[
        str | None,
        Field(
            description="Text for fill (replaces the field's value) or type (keystrokes appended to it)."
        ),
    ] = None,
    key: Annotated[
        str | None,
        Field(
            description="Key for press: a name such as Enter, Tab, Escape, Backspace, ArrowDown, or a combination like Control+A."
        ),
    ] = None,
    value: Annotated[
        str | None,
        Field(
            description="Option to choose for select: its value attribute, or its visible label with by_label."
        ),
    ] = None,
    by_label: Annotated[
        bool,
        Field(
            description="Match value against the option's visible label instead of its value attribute."
        ),
    ] = False,
    modifiers: Annotated[
        list[Literal["Alt", "Control", "Meta", "Shift"]] | None,
        Field(description="Modifier keys held during click, dblclick or press."),
    ] = None,
    delta_y: Annotated[
        float | None,
        Field(
            description="Pixels to scroll the page for scroll without target: positive down, negative up. Default: most of a screen down."
        ),
    ] = None,
    timeout: Annotated[
        float,
        Field(
            gt=0,
            description="Seconds allowed for the action, including finding the element (default 10).",
        ),
    ] = ELEMENT_TIMEOUT,
    snapshot: IncludeSnapshot = True,
) -> CallToolResult:
    """Perform one input action on the active tab with human-like mouse and keyboard, then
    settle and return the page state (url, navigated, changed) plus a fresh snapshot with
    @N refs. Use it for every click, form entry and keypress. Per action: click, dblclick,
    hover take target; fill takes target and text and replaces the value; type takes
    target and text and appends keystrokes (target '@focused' types into the focused
    element without clicking); press takes key; select takes target and value; scroll
    takes target (scrolls it into view) or delta_y, and with neither scrolls most of a
    screen down. changed: false in the result means nothing observable happened: snapshot
    or wait instead of repeating the action. Actions hit the live site and submissions have
    real side effects: stop before a payment or final confirmation the user did not ask for.
    """
    async with get_registry(ctx).acquire(session_id) as live:
        page = live.page
        before = await page.url()
        with anyio.fail_after(timeout):
            match action:
                case "click" | "dblclick" | "hover":
                    css = await resolve_target(live, require(target, action, "target"))
                    if action == "hover":
                        await page.hover(css, timeout=timeout)
                    elif action == "click":
                        await page.click(css, modifiers=modifiers, timeout=timeout)
                    else:
                        await page.dblclick(css, modifiers=modifiers, timeout=timeout)
                case "fill":
                    css = await resolve_target(live, require(target, action, "target"))
                    await page.fill(css, require(text, action, "text"))
                case "type":
                    words = require(text, action, "text")
                    target = require(target, action, "target")
                    if target == cli.FOCUSED:
                        await page.keyboard.type(words)
                    else:
                        await page.type(await resolve_target(live, target), words)
                case "press":
                    await page.keyboard.press(
                        require(key, action, "key"), modifiers=modifiers
                    )
                case "select":
                    css = await resolve_target(live, require(target, action, "target"))
                    chosen = require(value, action, "value")
                    try:
                        if by_label:
                            await page.select_option(css, label=chosen)
                        else:
                            await page.select_option(css, chosen)
                    except ValueError as exc:
                        raise NotFound(str(exc)) from exc
                case "scroll":
                    if target:
                        await page.scroll_into_view(await resolve_target(live, target))
                    else:
                        dy = delta_y
                        if dy is None:  # a reader paging: most of a screen, no page JS
                            metrics = await page.send("Page.getLayoutMetrics")
                            dy = round(
                                metrics["cssVisualViewport"]["clientHeight"]
                                * random.uniform(0.6, 0.9)
                            )
                        await page.scroll(delta_y=dy)
        return await action_result(live, before, want_snapshot=snapshot, action=True)


@tool("Wait for the page", READ_ONLY)
async def surfsky_wait(
    ctx: Context,
    session_id: SessionId = None,
    selector: Annotated[
        str | None,
        Field(
            description="Element to wait for: a CSS selector (works for elements that do not exist yet), @N, or text=words."
        ),
    ] = None,
    gone: Annotated[
        bool,
        Field(
            description="With selector: wait until it is absent or has no layout box, instead of present."
        ),
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
    actions already settle on their own, so no wait is needed after every step.
    Conditions: selector present (or gone), url_contains, text on the page, seconds; when
    several are given they are waited one after another, each with the full timeout. For
    elements that do not exist yet use a CSS selector: @N and text=words must resolve now.
    A condition not met in time fails with a timeout error and the page is left as it is.
    For a condition only script can check, use surfsky_evaluate with an async function
    that polls.
    """
    if not any([selector, url_contains, text, seconds]):
        raise UsageError("pass selector, url_contains, text or seconds")
    async with get_registry(ctx).acquire(session_id) as live:
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


@tool("Manage tabs", DESTRUCTIVE)
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
            description="Which tab for switch or close: the zero-based index from the last list, or its 8-character id (a prefix is enough)."
        ),
    ] = None,
    url: Annotated[
        str | None,
        Field(
            description="For new: URL to open in the new tab; https:// is added when the scheme is missing."
        ),
    ] = None,
) -> CallToolResult:
    """Manage the tabs of a session: list (index, id, url, title, active), new (opens a tab,
    optionally at url, and makes it active), switch (makes a tab active), close (a tab,
    default the active one). Use list then switch when a click opened a popup or a
    target=_blank link: the new page is another tab. Refs belong to a tab: take
    surfsky_snapshot after switching. The session's first tab cannot be closed; navigate
    it instead. The cloud allows five tabs per session.
    """
    async with get_registry(ctx).acquire(session_id) as live:
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


@tool("Scrape a URL", READ_ONLY)
async def surfsky_scrape(
    ctx: Context,
    url: Annotated[
        str,
        Field(description="URL to fetch; https:// is added when the scheme is missing."),
    ],
    formats: Annotated[
        list[Literal["markdown", "html", "links", "screenshot"]] | None,
        Field(
            description="One or more of markdown, html, links, screenshot (viewport JPEG). Default: markdown."
        ),
    ] = None,
    only_main_content: OnlyMain = False,
    wait_for: Annotated[
        str | None,
        Field(
            description="CSS selector to wait for before reading, for content that appears after the load event."
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
    proxy_type: ProxyType = None,
    os: OS = None,
    block_resources: Blocked = None,
) -> CallToolResult:
    """Fetch one URL in a throwaway cloud browser and return it as markdown (default),
    cleaned html, a list of links, and/or a viewport screenshot, with the final url, HTTP
    status and title. Use it whenever you only need to read a page: it starts no session,
    so there is nothing to stop, and it runs a real browser behind a residential proxy
    like the session tools. For a page that needs clicks, scrolling, a form or an interactive login, use
    surfsky_navigate, surfsky_act and surfsky_read; for a saved login pass profile_uuid
    here. A status of 200 with a short page may be a challenge page: read the content
    before trusting it. Text longer than max_chars is cut with a marker. Each call bills
    one short session.
    """
    if profile_uuid and os:
        raise UsageError("a profile owns its fingerprint: drop os, or drop profile_uuid")
    wanted = list(formats or ["markdown"])
    options = session_options(
        proxy, country, region, city, proxy_type, os, None, None, block_resources
    )
    client = get_registry(ctx).client()
    # The SDK stops the session on exit and shields that stop from cancellation.
    async with client.browser(profile_uuid=profile_uuid, **options) as browser:
        await browser.goto(with_scheme(url), wait_until=wait_until, timeout=timeout)
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
    text_formats = [name for name in wanted if name != "screenshot"]
    built = build(data, text_formats, main_content=only_main_content)
    header: dict[str, Any] = {"url": built["url"], "status": built["status"]}
    page: dict[str, Any] = {"title": built["title"]}
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
