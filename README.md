# surfsky-mcp

[![PyPI](https://img.shields.io/pypi/v/surfsky-mcp)](https://pypi.org/project/surfsky-mcp/)
[![CI](https://github.com/surfskyio/surfsky-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/surfskyio/surfsky-mcp/actions/workflows/ci.yml)

Chrome in the cloud for Claude, Cursor or any MCP client. Read pages a plain HTTP
fetch cannot, then click through them, fill forms and take screenshots. Runs on
[Surfsky](https://surfsky.io), an antidetect browser with residential proxies and
human-like input.

## Install

Requires Python 3.12+, `uv`, and a Surfsky account. Replace `...` with your
`SURFSKY_API_TOKEN` and `SURFSKY_API_BASE_URL` from the
[dashboard](https://app.surfsky.io). Browser time and proxy traffic are billed.

|                       | macOS                                                             | Windows                                       | Linux                                              |
| --------------------- | ----------------------------------------------------------------- | --------------------------------------------- | -------------------------------------------------- |
| Install `uv`          | `brew install uv`                                                 | `winget install astral-sh.uv`                 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Claude Desktop config | `~/Library/Application Support/Claude/claude_desktop_config.json` | `%APPDATA%\Claude\claude_desktop_config.json` | no Desktop build; use Claude Code                  |
| Claude Desktop logs   | `~/Library/Logs/Claude/`                                          | `%APPDATA%\Claude\logs\`                      |                                                    |

`uv` downloads Python itself, so no separate Python install is needed.

Claude Code:

```sh
claude mcp add surfsky -e SURFSKY_API_TOKEN=... -e SURFSKY_API_BASE_URL=... -- uvx surfsky-mcp
```

Claude Desktop / Cursor:

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

On macOS the app does not read your shell profile, so `"command": "uvx"` fails
to start. Use the absolute path that `which uvx` prints. Windows finds `uvx` on
PATH.

After changing the config, quit Claude Desktop with Cmd+Q on macOS or from the
tray on Windows, then start it again. Closing the window leaves the old server
process running.

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.surfsky]
command = "uvx"
args = ["surfsky-mcp"]
env = { SURFSKY_API_TOKEN = "...", SURFSKY_API_BASE_URL = "..." }
```

## Configuration

| Variable               | Default      | Meaning                                                        |
| ---------------------- | ------------ | -------------------------------------------------------------- |
| `SURFSKY_API_TOKEN`    |              | Required. From the [dashboard](https://app.surfsky.io)         |
| `SURFSKY_API_BASE_URL` |              | Required. The API host for your account, also on the dashboard |
| `SURFSKY_HOME`         | `~/.surfsky` | Where session state files are written                          |

Set them in the MCP client's config, not in your shell.

Logs go to stderr; stdout carries JSON-RPC only.

## What you can ask

| Ask                                                      | What happens                                                                                                                                                       |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| "Read this page and summarize the reviews."              | One `surfsky_scrape`. Starts no session, so nothing to stop                                                                                                        |
| "Compare this product's price across these three shops." | Three scrapes. A shop that needs a click to show the price gets a session                                                                                          |
| "Fill this form with the details from my notes."         | `surfsky_snapshot` for `@N` references, then `surfsky_act`. The server tells the model to stop short of a payment or a final confirmation unless you asked for one |
| "Sign in and check last month's invoice."                | `surfsky_session_start` with a `profile_uuid` you supply; the saved profile carries the login                                                                      |
| "Screenshot the top of this article."                    | `surfsky_screenshot`, or `surfsky_scrape` with `formats: ["screenshot"]`                                                                                           |
| "Which of my browsers are still running?"                | `surfsky_session_list`, then `surfsky_session_stop` for the ones you are done with                                                                                 |

## Tools

| Tool                    | Description                                                           |
| ----------------------- | --------------------------------------------------------------------- |
| `surfsky_scrape`        | Fetch a URL as Markdown, HTML, links or a screenshot.                 |
| `surfsky_navigate`      | Open a URL, go back or forward, or reload.                            |
| `surfsky_snapshot`      | List page elements with `@N` references for actions.                  |
| `surfsky_act`           | Click, double-click, hover, fill, type, press keys, select or scroll. |
| `surfsky_wait`          | Wait for an element, URL, text or a delay.                            |
| `surfsky_read`          | Read page content as Markdown, text, HTML or links.                   |
| `surfsky_screenshot`    | Capture the viewport, full page or an element.                        |
| `surfsky_evaluate`      | Run JavaScript in an isolated context or the page context.            |
| `surfsky_tab`           | List, open, switch or close tabs.                                     |
| `surfsky_session_start` | Start a session with profile, proxy or fingerprint settings.          |
| `surfsky_session_stop`  | Stop a session and its billing. Safe to repeat.                       |
| `surfsky_session_list`  | List the account's running sessions.                                  |
| `surfsky_status`        | Check credentials, plan limits and proxy quota.                       |

## Sessions

Browser tools share a default session unless you pass `session_id`. It starts
on first use and stops after five minutes idle or on server shutdown. Call
`surfsky_session_stop` to stop billing immediately.

Use `surfsky_session_start` for separate sessions, saved profiles or custom
proxy and fingerprint settings. `surfsky_scrape` stops its browser automatically.
Sessions can also be listed and stopped through the
[Surfsky CLI](https://github.com/surfskyio/surfsky-cli).

## HTTP mode

```sh
uvx surfsky-mcp --http --host 0.0.0.0 --port 8000 --allowed-hosts mcp.example.com
```

Serves streamable HTTP at `/mcp`. Set the same credentials in the environment.
There is no built-in authentication; use an authenticated proxy or private network.
Set `--allowed-hosts` to the hostname clients use. Without it, non-localhost
binds disable Host-header checks.

Clients sharing a server must use separate `session_id` values.

## Troubleshooting

**A page returns 200 but the text is short, or asks you to continue shopping or
prove you are human.** That is a bot challenge, not a bug. Each scrape gets a
fresh browser and a different proxy exit, so a retry often gets through. If a
site blocks every fresh browser, start a session with a saved `profile_uuid`,
whose cookies survive between runs.

**The result carries `note: navigation did not reach 'load'`.** The document
parsed, but a request stayed open past the timeout. The content and the status
are there; anything that loads late may be missing. Pass
`wait_until: "domcontentloaded"` and a `wait_for` selector for the element you
need.

**`error [timeout]`.** The navigation committed no document at all. Retry with a
longer `timeout`, or `wait_until: "commit"` to stop at the first response.

**`error [stale_ref]`.** The page moved under the `@N` references. Call
`surfsky_snapshot` and pick the target again.

**`error [session_gone]`.** The cloud no longer has that session. Start another;
page state is lost.

**`error [plan_full]` or `error [quota]`.** The plan's parallel sessions or its
traffic are used up. `surfsky_session_list` shows what is running and
`surfsky_session_stop` frees one. `surfsky_status` shows the limits. Do not retry
in a loop.

**`error [auth]`.** The token or the base URL never reached the server process.
Check the `env` block of the MCP client's config, not your shell.

**A session keeps billing.** The shared default session stops after five minutes
idle and on server shutdown. `surfsky_session_stop` ends it now, and the
[Surfsky CLI](https://github.com/surfskyio/surfsky-cli) can stop sessions this
server does not know about.

## Security and privacy

The server stores nothing. It sends the URLs and actions a tool call names to the
Surfsky API, which runs the browser, and returns what the page said to your MCP
client. The token is read from the environment and goes nowhere but that API.

Page content, snapshots, screenshots and JavaScript results arrive inside an
`<untrusted-page-content>` block introduced as data rather than instructions.
Invisible and bidirectional characters are stripped, and a page cannot close the
block early. Filtering does not catch every prompt injection. Check what an
action does before it submits a form or spends money.

A `profile_uuid` carries real logins. Pass one only when the user gave it to you.
HTTP mode has no authentication of its own; see [HTTP mode](#http-mode).

## Development

```sh
uv sync --all-groups
uv run ruff check . && uv run ty check && uv run pytest -q
SURFSKY_LIVE_TESTS=1 uv run pytest tests/test_live.py   # billed
```

## License

MIT
