# surfsky-mcp

[![PyPI](https://img.shields.io/pypi/v/surfsky-mcp)](https://pypi.org/project/surfsky-mcp/)
[![CI](https://github.com/surfskyio/surfsky-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/surfskyio/surfsky-mcp/actions/workflows/ci.yml)

MCP server for the [Surfsky](https://surfsky.io) cloud browser: an agent reads a
URL in one call, or controls a session (navigate, click, type, screenshot) in an
antidetect Chrome behind residential proxies.

## Connect

Surfsky hosts the server at `https://mcp.surfsky.io/mcp`. You need a Surfsky
account. Browser time, scrapes and proxy traffic are billed.

Claude Code, OAuth login:

```sh
claude mcp add --transport http surfsky https://mcp.surfsky.io/mcp
claude mcp login surfsky
```

The login opens app.surfsky.io to sign in and allow access. Any other client
with OAuth support takes the URL alone. Revoke access under Connected apps in the
[dashboard](https://app.surfsky.io).

Claude Code, API token from the dashboard:

```sh
claude mcp add --transport http surfsky https://mcp.surfsky.io/mcp --header "Authorization: Bearer YOUR_API_TOKEN"
```

Cursor and other clients that take a URL with headers:

```json
{
  "mcpServers": {
    "surfsky": {
      "url": "https://mcp.surfsky.io/mcp",
      "headers": { "Authorization": "Bearer YOUR_API_TOKEN" }
    }
  }
}
```

The hosted server finds your account's API host itself. Browser tools need
`surfsky_session_start` first; see [Sessions](#sessions).

## Run locally

Needs [`uv`](https://docs.astral.sh/uv/), which downloads Python 3.12+ itself.

| Variable               | Default      | Meaning                                      |
| ---------------------- | ------------ | -------------------------------------------- |
| `SURFSKY_API_TOKEN`    |              | Required. From the dashboard                 |
| `SURFSKY_API_BASE_URL` |              | Required. Your account's API host, same page |
| `SURFSKY_HOME`         | `~/.surfsky` | Where session records are written            |

Set them in the MCP client's config; the server does not see your shell profile.
Logs go to stderr.

Claude Code:

```sh
claude mcp add surfsky -e SURFSKY_API_TOKEN=... -e SURFSKY_API_BASE_URL=... -- uvx surfsky-mcp
```

Claude Desktop and Cursor:

```json
{
  "mcpServers": {
    "surfsky": {
      "command": "uvx",
      "args": ["surfsky-mcp"],
      "env": { "SURFSKY_API_TOKEN": "...", "SURFSKY_API_BASE_URL": "..." }
    }
  }
}
```

Codex, `~/.codex/config.toml`:

```toml
[mcp_servers.surfsky]
command = "uvx"
args = ["surfsky-mcp"]
env = { SURFSKY_API_TOKEN = "...", SURFSKY_API_BASE_URL = "..." }
```

Claude Desktop:

- Config: `~/Library/Application Support/Claude/claude_desktop_config.json` on
  macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows. Logs:
  `~/Library/Logs/Claude/` and `%APPDATA%\Claude\logs\`.
- On macOS the app has no shell PATH, so `"command": "uvx"` fails to start. Use
  the absolute path from `which uvx`.
- After a config change quit the app (Cmd+Q, or the tray on Windows). Closing
  the window leaves the old server process running.

## Tools

| Tool                    | Does                                                                |
| ----------------------- | ------------------------------------------------------------------- |
| `surfsky_scrape`        | Fetch a URL as markdown, html, links or a screenshot. No session    |
| `surfsky_session_start` | Start a session: saved profile, proxy, location, fingerprint OS     |
| `surfsky_session_stop`  | Stop any session on the account and its billing. Safe to repeat     |
| `surfsky_session_list`  | List the account's running sessions                                 |
| `surfsky_navigate`      | Open a URL, go back or forward, or reload                           |
| `surfsky_snapshot`      | List visible interactive elements and headings as `@N` refs         |
| `surfsky_act`           | click, dblclick, hover, fill, type, press, select, scroll           |
| `surfsky_wait`          | Wait for an element, a URL substring, page text or a fixed delay    |
| `surfsky_read`          | Read the tab as markdown, text, html or links                       |
| `surfsky_screenshot`    | Viewport as JPEG; full page or one element as PNG                   |
| `surfsky_evaluate`      | Run JavaScript, in an isolated world unless `main_world` is set     |
| `surfsky_tab`           | List, open, switch or close tabs                                    |
| `surfsky_status`        | Credentials, plan limits, proxy quota, sessions this server owns    |

Targets are a CSS selector, `@N` from a snapshot of the same tab and URL, or
`text=words`. No XPath. Iframe and shadow-root contents are not in the snapshot.

`surfsky_act` with `steps` runs up to 20 actions in one call, within 45 seconds.
The first failing step stops the run; the error names it and carries the page
state.

`surfsky_scrape` goes through the Scraping API, which serves the page from a
pre-started browser. With `profile_uuid`, `proxy`, `country`, `region`, `city`
or `os` it starts a browser of its own for the call and stops it after.

## Sessions

A session bills per minute until `surfsky_session_stop` or its idle timeout: 300
seconds without a tool call by default, `idle_timeout` on start sets 1 to 3600.
Each one counts against the plan's parallel browsers. The server stops its
sessions on shutdown.

Local server: browser tools without `session_id` share a default session that
starts on first use. Start your own for a saved profile, a proxy location, a
fingerprint OS or a second browser.

Hosted server: there is no default session. Browser tools use the session your
client last started, and `session_id` picks another. A client that keeps no
connection between calls gets the account's only running session and must pass
`session_id` when several run.

`surfsky_session_start` returns a `live_url` to watch the browser and take over
for a login, a 2FA code or a captcha. Anyone with the URL controls the browser.

A `profile_uuid` loads a saved profile; its cookies and logins are saved back on
a normal stop. It cannot be combined with `os`, `os_version` or `arch`.

The [Surfsky CLI](https://github.com/surfskyio/surfsky-cli) also lists and stops
sessions, including ones this server did not start.

## Bot challenges

`surfsky_scrape` passes most challenge pages itself and each call gets a
different proxy exit, so a retry often gets through.

`surfsky_navigate` makes at most one solve of a challenge page's own widget,
billed as a captcha solve where the account pays for solving. The result's `note`
reads `challenge_page met, solved`, `passed` (the page let the browser through
without a solve) or `not solved`, with the price when one was charged. It never
solves a captcha on an ordinary page.

A 200 with a short page, or one asking to prove you are human, is still a
challenge. If a site blocks every fresh browser, use a session with a saved
`profile_uuid`.

## Limits

| What                                             | Limit                                         |
| ------------------------------------------------ | --------------------------------------------- |
| Text from `surfsky_scrape` and `surfsky_read`    | 10,000 chars; `max_chars` up to 200,000       |
| `surfsky_evaluate` result                        | 30,000 chars                                  |
| `surfsky_snapshot` rows                          | 500; `limit` raises it, `find` narrows        |
| Snapshot in `surfsky_navigate` and `surfsky_act` | first 80 rows                                 |
| `surfsky_read` with `selector` and `limit`       | 100 elements                                  |
| `surfsky_act` `steps`                            | 20 actions, 45 s per call                     |
| `surfsky_act` `timeout`, per action              | 10 s, at most 30                              |
| `timeout` of navigate, scrape and wait           | 30 s by default                               |
| Tabs per session                                 | 5                                             |

Cut text ends with a `[truncated: ...]` marker.

## HTTP mode

```sh
uvx surfsky-mcp --http --host 0.0.0.0 --port 8000 --allowed-hosts mcp.example.com
```

Serves streamable HTTP at `/mcp` for one account, with the same environment
variables as stdio. Defaults are `127.0.0.1:8000`. There is no authentication:
put it behind an authenticating proxy or a private network. `--allowed-hosts` is
a comma-separated list of Host header values clients use; without it a
non-localhost bind turns Host header checks off. Clients sharing the server
should each start their own session.

### Hosted mode

`--hosted` is what runs at `mcp.surfsky.io`: one server for many accounts. Each
request carries an API token as a bearer and gets 401 without a valid one.
Sessions and tokens live in memory, so run one replica.

| Variable                      | Meaning                                                              |
| ----------------------------- | -------------------------------------------------------------------- |
| `SURFSKY_API_BASE_URL`        | Required. API host that looks accounts up                            |
| `SURFSKY_REGIONS`             | Clusters to API hosts, as JSON: `{"de2": {"url": "https://..."}}`    |
| `SURFSKY_OAUTH`               | Set to `1` to add OAuth login; needs the four variables below        |
| `SURFSKY_MCP_URL`             | Public URL of this server without `/mcp`, HTTPS unless localhost     |
| `SURFSKY_CONSENT_URL`         | Consent page users are sent to                                       |
| `SURFSKY_WEB_APP_URL`         | Dashboard API that stores OAuth clients and connected apps           |
| `SURFSKY_MCP_INTERNAL_SECRET` | Bearer secret for that API                                           |

With OAuth on, API tokens in the header still work. Access tokens last an hour.
The dashboard keeps refresh tokens, so after a restart clients get a new access
token without signing in again.

## Troubleshooting

Errors come back as `error [code]: message` with a hint.

| Code or note                           | Cause and fix                                                                                             |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `auth`                                 | Token or base URL did not reach the server process: check the `env` block of the client config. The hosted server answers a bad token with HTTP 401 at connect |
| `no_session`                           | Hosted: nothing is running, or several are and the call named none. Call `surfsky_session_start` or pass `session_id` |
| `session_gone`                         | The session stopped, idled out, or the server restarted. Start another; page state is lost                |
| `stale_ref`                            | The page changed under the `@N` refs. Call `surfsky_snapshot` and pick the target again                   |
| `timeout`                              | Nothing arrived in time. Retry with a longer `timeout`, or `wait_until: "domcontentloaded"` or `"commit"`                          |
| `plan_full`, `quota`                   | Parallel sessions or traffic are used up. `surfsky_session_list` and `surfsky_session_stop` free a slot; `surfsky_status` shows the limits. Do not retry in a loop |
| `insufficient_balance`, `plan_limit`   | Top up, or change the plan, in the dashboard                                                              |
| `note: navigation did not reach 'load'` | A request stayed open past the timeout. Status and content are there, late content may be missing. Use `wait_until: "domcontentloaded"` and `surfsky_wait` (`wait_for` in `surfsky_scrape`) for the element you need |

## Security

Tool calls go to the Surfsky API, which runs the browser, and nowhere else. The
local server writes only the records of its running sessions under
`SURFSKY_HOME`; the hosted server writes nothing to disk, takes the token in a
header and logs accounts by a hash of it.

Page content, snapshots, screenshots and JavaScript results are untrusted. Text
arrives inside an `<untrusted-page-content>` block with invisible and
bidirectional characters stripped, and a page cannot close the block early. That
does not stop every prompt injection: check what an action does before it
submits a form or spends money. The server's instructions tell the model to stop
before a payment or final confirmation the user did not ask for.

A `profile_uuid` carries real logins. Pass one only when the user gave it.

## Development

```sh
uv sync --all-groups
uv run ruff check . && uv run ty check && uv run pytest -q
SURFSKY_LIVE_TESTS=1 uv run pytest tests/test_live.py   # billed
```

## License

MIT
