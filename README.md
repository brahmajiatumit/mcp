# SAP Integration Suite MCP – Python tools

Python 3.9+ and the standard library only; nothing to install.

| File | What it is |
|---|---|
| `sap_mcp_client.py` | Client library + CLI: gets the XSUAA token (client credentials), opens the MCP session, lists and calls tools. Refreshes the token and re-opens the session automatically. |
| `sap_mcp_proxy.py` | Local stdio MCP server that forwards to SAP. Lets Claude Code / Claude Desktop use the SAP MCP server, since claude.ai connectors can't use client credentials. |
| `mpl_reports.py` | Ready-made reports: top failing iflows with their errors, and the quietest hours for planning upgrades. |

## Configure

```bash
export SAP_MCP_URL=https://<tenant>.a.integration.cloud.sap/Mplmcp
export SAP_TOKEN_URL=https://<subaccount>.authentication.eu10.hana.ondemand.com/oauth/token
export SAP_CLIENT_ID='<clientid from the service key>'
export SAP_CLIENT_SECRET='<clientsecret from the service key>'
```

The service key must belong to a Process Integration Runtime instance (plan `integration-flow`)
with the `ESBMessaging.send` role. Use single quotes: the client ID contains `!` and `|`, and the
secret contains `$`.

## Use

```bash
python3 sap_mcp_client.py tools
python3 sap_mcp_client.py call get_MessageProcessingLogs_count '{"filter": "Status eq '\''FAILED'\''"}'

python3 mpl_reports.py failed --hours 24 --top 5
python3 mpl_reports.py quiet --days 7
```

In your own code:

```python
from sap_mcp_client import SapMcpClient

client = SapMcpClient.from_env()
print(client.call_tool_text("get_MessageProcessingLogs_count", {"filter": "Status eq 'FAILED'"}))
```

## Post reports to Microsoft Teams

1. In the Teams channel: **⋯ → Workflows → "Post to a channel when a webhook request is received"**,
   finish the wizard and copy the webhook URL it gives you.
2. Run a report with `--teams`:

```bash
export TEAMS_WEBHOOK_URL='https://...'   # keep it secret: anyone with the URL can post to the channel
python3 mpl_reports.py failed --hours 24 --teams
```

The report is posted as an Adaptive Card table. Schedule it with cron (or any job scheduler) for a daily
summary, e.g. `0 8 * * 1-5 cd /path/to/sap_mcp && python3 mpl_reports.py failed --hours 24 --teams`.

## Use from Claude Code

```bash
claude mcp add sap-mpl \
  -e SAP_MCP_URL=... -e SAP_TOKEN_URL=... -e SAP_CLIENT_ID='...' -e SAP_CLIENT_SECRET='...' \
  -- python3 /full/path/to/sap_mcp_proxy.py
```

For Claude Desktop, add the same command and `env` values under `mcpServers` in
`claude_desktop_config.json`. The same proxy works for any other SAP MCP server (for example
`/petMCP`); just change `SAP_MCP_URL`.
