import json
import signal
import subprocess
import sys
import time

from mcp.server.transport_security import TransportSecuritySettings

from surfsky_mcp import server

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "x", "version": "0"},
    },
}


def test_instructions_fit_the_host_budget():
    assert len(server.INSTRUCTIONS.encode()) <= 2048
    for name in (
        "surfsky_scrape",
        "surfsky_session_stop",
        "surfsky_snapshot",
        "untrusted",
    ):
        assert name in server.INSTRUCTIONS


def test_transport_security_settings():
    assert server.transport_security("127.0.0.1", []) is None
    custom = server.transport_security("0.0.0.0", ["mcp.example.com"])
    assert isinstance(custom, TransportSecuritySettings)
    assert custom.allowed_hosts == ["mcp.example.com", "mcp.example.com:*"]
    open_ = server.transport_security("0.0.0.0", [])
    assert open_ is not None and open_.enable_dns_rebinding_protection is False


def test_version_flag():
    out = subprocess.run(
        [sys.executable, "-m", "surfsky_mcp", "--version"], capture_output=True, text=True
    )
    assert out.returncode == 0 and out.stdout.startswith("surfsky-mcp ")


def test_sigterm_runs_lifespan_cleanup_and_exits_zero():
    proc = subprocess.Popen(
        [sys.executable, "-m", "surfsky_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write((json.dumps(INITIALIZE) + "\n").encode())
        proc.stdin.flush()
        time.sleep(1)
        proc.send_signal(signal.SIGTERM)
        proc.stdin.close()
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
