"""Local stdio MCP proxy for an SAP Integration Suite MCP server.

claude.ai connectors only support interactive OAuth sign-in, but SAP-hosted MCP
servers use the client credentials grant. This proxy bridges the two: an MCP
client (Claude Code, Claude Desktop, ...) starts it as a local stdio server, and
it forwards every message to SAP, adding a fresh XSUAA token and the MCP session id.

Claude Code:
    claude mcp add sap-mpl \\
        -e SAP_MCP_URL=https://<tenant>.a.integration.cloud.sap/Mplmcp \\
        -e SAP_TOKEN_URL=https://<subaccount>.authentication.eu10.hana.ondemand.com/oauth/token \\
        -e SAP_CLIENT_ID=... -e SAP_CLIENT_SECRET=... \\
        -- python3 /path/to/sap_mcp_proxy.py

Logs go to stderr; stdout carries only MCP messages.
"""

from __future__ import annotations

import json
import sys

from sap_mcp_client import McpError, SapMcpClient, _header


def log(msg: str) -> None:
    print(f"[sap-mcp-proxy] {msg}", file=sys.stderr, flush=True)


def write(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    client = SapMcpClient.from_env()
    log(f"forwarding to {client.url}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue

        is_request = "id" in message and "method" in message
        try:
            if message.get("method") == "initialize":
                client.session_id = None  # the client is starting over
            _, headers, replies = client.post(message)
            if message.get("method") == "initialize":
                client.session_id = _header(headers, "Mcp-Session-Id")
                log(f"session {client.session_id}")
            for reply in replies:
                write(reply)
        except McpError as e:
            log(str(e))
            if is_request:
                write({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": str(e)}})


if __name__ == "__main__":
    main()
