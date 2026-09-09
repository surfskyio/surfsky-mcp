# surfsky-mcp

MCP server for [Surfsky](https://surfsky.io), a cloud-based antidetect browser.

## Install

Requires Python 3.12+, `uv`, and a Surfsky account. Replace `...` with your
`SURFSKY_API_TOKEN` and `SURFSKY_API_BASE_URL` from the
[dashboard](https://app.surfsky.io). Browser time and proxy traffic are billed.

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

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.surfsky]
command = "uvx"
args = ["surfsky-mcp"]
env = { SURFSKY_API_TOKEN = "...", SURFSKY_API_BASE_URL = "..." }
```

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

## Development

```sh
uv sync --all-groups
uv run ruff check . && uv run ty check && uv run pytest -q
SURFSKY_LIVE_TESTS=1 uv run pytest tests/test_live.py   # billed
```

## License

MIT
