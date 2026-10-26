"""Client for an MCP server hosted on SAP Integration Suite (Cloud Integration).

SAP-hosted MCP servers are protected by XSUAA with the OAuth *client credentials*
grant. This module fetches and caches the token, opens an MCP session
(streamable HTTP transport) and lets you list and call tools.

Standard library only. Configuration comes from environment variables:

    SAP_MCP_URL        e.g. https://<tenant>.a.integration.cloud.sap/Mplmcp
    SAP_TOKEN_URL      e.g. https://<subaccount>.authentication.eu10.hana.ondemand.com/oauth/token
    SAP_CLIENT_ID      clientid from the Process Integration Runtime service key
    SAP_CLIENT_SECRET  clientsecret from the same service key

The service key's client needs the ESBMessaging.send role (scope ...API.invoke).

Command line:
    python sap_mcp_client.py tools
    python sap_mcp_client.py call get_MessageProcessingLogs_count '{"filter": "Status eq '\''FAILED'\''"}'
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"


class McpError(Exception):
    """Raised when the server returns an HTTP error or a JSON-RPC error."""


class TokenProvider:
    """Fetches XSUAA tokens with client credentials and caches them until shortly before expiry."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, timeout: float = 30):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or not self._token or time.time() > self._expires_at - 60:
                self._fetch()
            return self._token

    def _fetch(self) -> None:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            self.token_url,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            raise McpError(f"token request failed: HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from e
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))


class SapMcpClient:
    """Minimal MCP client (streamable HTTP) for SAP Integration Suite MCP servers."""

    def __init__(self, url: str, tokens: TokenProvider, timeout: float = 120):
        self.url = url
        self.tokens = tokens
        self.timeout = timeout
        self.session_id: str | None = None
        self.server_info: dict | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "SapMcpClient":
        missing = [v for v in ("SAP_MCP_URL", "SAP_TOKEN_URL", "SAP_CLIENT_ID", "SAP_CLIENT_SECRET") if not os.environ.get(v)]
        if missing:
            raise McpError("missing environment variables: " + ", ".join(missing))
        tokens = TokenProvider(os.environ["SAP_TOKEN_URL"], os.environ["SAP_CLIENT_ID"], os.environ["SAP_CLIENT_SECRET"])
        return cls(os.environ["SAP_MCP_URL"], tokens)

    # ---- low level ---------------------------------------------------------

    def post(self, message: dict) -> tuple[int, dict, list[dict]]:
        """POST one JSON-RPC message. Returns (status, headers, JSON-RPC messages in the reply).

        Retries once with a fresh token on 401, and once with a new session if the
        server has dropped ours (404/400 while a session id is set).
        """
        for attempt in range(3):
            headers = {
                "Authorization": f"Bearer {self.tokens.get(force_refresh=attempt > 0)}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            }
            if self.session_id and message.get("method") != "initialize":
                headers["Mcp-Session-Id"] = self.session_id
            req = urllib.request.Request(self.url, data=json.dumps(message).encode(), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, dict(resp.headers), _parse_body(resp.read(), resp.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code == 401 and attempt == 0:
                    continue
                if e.code in (400, 404) and self.session_id and message.get("method") != "initialize" and attempt < 2:
                    self.session_id = None
                    self.initialize()
                    continue
                raise McpError(f"HTTP {e.code} from MCP server: {body[:500]}") from e
        raise McpError("request failed after retries")

    def request(self, method: str, params: dict | None = None) -> dict:
        if not self.session_id and method != "initialize":
            self.initialize()
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        _, headers, replies = self.post(message)
        if method == "initialize":
            self.session_id = _header(headers, "Mcp-Session-Id")
        for reply in replies:
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise McpError(f"{method} failed: {reply['error']}")
                return reply.get("result", {})
        raise McpError(f"no response to {method} (got {replies!r:.300})")

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.post(message)

    # ---- MCP operations ----------------------------------------------------

    def initialize(self) -> dict:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "sap-mcp-client", "version": "1.0"},
        })
        self.server_info = result.get("serverInfo")
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools", [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Calls a tool and returns the raw MCP result ({"content": [...], "isError": ...})."""
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def call_tool_text(self, name: str, arguments: dict | None = None) -> str:
        """Calls a tool and returns its text output; raises McpError if the tool reported an error."""
        result = self.call_tool(name, arguments)
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise McpError(f"tool {name} failed: {text[:500]}")
        return text


def _header(headers: dict, name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


def _parse_body(raw: bytes, content_type: str) -> list[dict]:
    """Returns the JSON-RPC messages in a reply, which may be plain JSON or a server-sent event stream."""
    text = raw.decode(errors="replace").strip()
    if not text:
        return []
    if "text/event-stream" in content_type:
        messages, data = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                messages.append(json.loads("\n".join(data)))
                data = []
        return messages
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("tools", "call"):
        print(__doc__)
        return 2
    client = SapMcpClient.from_env()
    info = client.initialize()
    print(f"connected to {info.get('serverInfo', {}).get('name')}", file=sys.stderr)
    if argv[1] == "tools":
        for tool in client.list_tools():
            print(f"{tool['name']}: {tool.get('description', '').strip().splitlines()[0] if tool.get('description') else ''}")
    else:
        args = json.loads(argv[3]) if len(argv) > 3 else {}
        print(client.call_tool_text(argv[2], args))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
