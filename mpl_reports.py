"""Message Processing Log reports built on the Mplmcp MCP server.

    python mpl_reports.py failed --hours 24 --top 5     # top failing iflows (+ latest error text)
    python mpl_reports.py quiet --days 7                # quietest 1-hour windows, for planning upgrades
    python mpl_reports.py failed --teams                # ...and post it to Teams

Needs the same environment variables as sap_mcp_client.py, plus TEAMS_WEBHOOK_URL for --teams.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

from sap_mcp_client import SapMcpClient

PAGE = 1000
UK = ZoneInfo("Europe/London")


def odata_time(t: dt.datetime) -> str:
    return "datetime'" + t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "'"


def parse_date(value: str) -> dt.datetime:
    """Converts an OData '/Date(1792505789959)/' value to an aware datetime."""
    return dt.datetime.fromtimestamp(int(re.search(r"\d+", value).group()) / 1000, dt.timezone.utc)


def fetch_logs(client: SapMcpClient, filter_: str, select: list[str], orderby: str = "LogStart") -> list[dict]:
    """Pages through get_MessageProcessingLogs and returns every matching log."""
    rows, skip = [], 0
    while True:
        text = client.call_tool_text("get_MessageProcessingLogs", {
            "filter": filter_, "select": select, "orderby": [orderby], "top": PAGE, "skip": skip,
        })
        page = json.loads(text)["d"]["results"]
        rows += page
        skip += len(page)
        print(f"\rfetched {skip} logs", end="", file=sys.stderr)
        if len(page) < PAGE:
            print(file=sys.stderr)
            return rows


def error_text(client: SapMcpClient, guid: str) -> str:
    try:
        text = " ".join(client.call_tool_text("get_MessageProcessingLogs_MessageGuid_ErrorInformation_value",
                                              {"MessageGuid": guid}).split())
    except Exception as e:  # error details are best effort
        return f"(could not read error: {e})"
    # Shorten the common Camel HTTP failure to its useful part.
    m = re.search(r"invoking (\S+?)(?:\?\S*)? with statusCode: (\d+)", text)
    return f"HTTP {m.group(2)} from {m.group(1)}" if m else text


def report_failed(client: SapMcpClient, hours: float, top: int) -> None:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    rows = fetch_logs(client, f"Status eq 'FAILED' and LogEnd ge {odata_time(since)}",
                      ["IntegrationFlowName", "MessageGuid", "LogEnd"], orderby="LogEnd desc")
    by_flow = collections.defaultdict(list)
    for r in rows:
        by_flow[r["IntegrationFlowName"]].append(r)
    ranked = sorted(by_flow.items(), key=lambda kv: -len(kv[1]))[:top]

    title = f"Failed messages in the last {hours:g} h: {len(rows):,} across {len(by_flow)} iflows"
    columns = ["#", "Integration flow", "Failed", "Share", "Latest (UK)", "Latest error"]
    table = []
    for i, (flow, logs) in enumerate(ranked, 1):
        latest = parse_date(logs[0]["LogEnd"]).astimezone(UK).strftime("%d %b %H:%M")
        err = error_text(client, logs[0]["MessageGuid"])[:160]
        table.append([str(i), flow, f"{len(logs):,}", f"{len(logs) / len(rows):.1%}", latest, err])
    print_table(title, columns, table)
    return title, columns, table


def print_table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    print(f"\n{title}\n")
    print("| " + " | ".join(columns) + " |")
    print("|" + "---|" * len(columns))
    for row in rows:
        print("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")


def post_to_teams(webhook_url: str, title: str, columns: list[str], rows: list[list[str]]) -> None:
    """Posts the table as an Adaptive Card to a Teams Workflows webhook
    ("Post to a channel when a webhook request is received")."""
    def cells(values, bold=False):
        return [{"type": "TableCell", "items": [{"type": "TextBlock", "text": v, "wrap": True,
                                                  **({"weight": "Bolder"} if bold else {})}]} for v in values]
    card = {
        "type": "AdaptiveCard", "version": "1.5",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "msteams": {"width": "Full"},
        "body": [
            {"type": "TextBlock", "text": "SAP Integration Suite report", "weight": "Bolder", "size": "Large"},
            {"type": "TextBlock", "text": title, "wrap": True, "isSubtle": True},
            {"type": "Table", "firstRowAsHeaders": True, "showGridLines": True,
             "columns": [{"width": 1} for _ in columns],
             "rows": [{"type": "TableRow", "cells": cells(columns, bold=True)}]
                     + [{"type": "TableRow", "cells": cells(r)} for r in rows]},
            {"type": "TextBlock", "text": f"Generated {dt.datetime.now(UK):%d %b %Y %H:%M} UK", "size": "Small", "isSubtle": True},
        ],
    }
    payload = {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
    req = urllib.request.Request(webhook_url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"posted to Teams (HTTP {resp.status})", file=sys.stderr)


def report_quiet(client: SapMcpClient, days: int, show: int) -> None:
    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)  # skip the partial hour
    start = end - dt.timedelta(days=days)
    rows = fetch_logs(client, f"LogStart ge {odata_time(start)}", ["LogStart"])
    times = sorted(t for t in (parse_date(r["LogStart"]) for r in rows) if start <= t < end)
    hours = int((end - start).total_seconds() // 3600)

    buckets = collections.Counter(int((t - start).total_seconds() // 3600) for t in times)
    per_hour = sorted((buckets.get(i, 0), start + dt.timedelta(hours=i)) for i in range(hours))
    print(f"\n{len(times):,} messages over {days} days, average {len(times) / hours:.0f}/hour, "
          f"busiest hour {per_hour[-1][0]:,}\n")

    table = []
    for count, h in per_hour[:show]:
        h = h.astimezone(UK)
        table.append([f"{h:%a %d %b %H:%M}–{h + dt.timedelta(hours=1):%H:%M}", f"{count:,}"])
    title = (f"Quietest hours over the last {days} days ({len(times):,} messages, "
             f"average {len(times) / hours:.0f}/hour)")
    print_table(title, ["Window (UK)", "Messages"], table)

    # Quietest 60-minute window starting on any minute.
    stamps = [t.timestamp() for t in times]
    best = min(
        (bisect.bisect_left(stamps, s + 3600) - bisect.bisect_left(stamps, s), s)
        for s in (start.timestamp() + m * 60 for m in range(hours * 60 - 59))
    )
    w = dt.datetime.fromtimestamp(best[1], UK)
    print(f"\nQuietest rolling hour: {w:%a %d %b %H:%M}–{w + dt.timedelta(hours=1):%H:%M} UK, {best[0]:,} messages")

    by_hod = collections.Counter()
    for count, h in per_hour:
        by_hod[h.astimezone(UK).hour] += count
    print("\nAverage messages by hour of day (UK):\n\n| Hour | Avg/hour |\n|---|---:|")
    for hod in range(24):
        print(f"| {hod:02d}:00 | {by_hod[hod] / days:.0f} |")
    table.append([f"Quietest rolling hour: {w:%a %d %b %H:%M}–{w + dt.timedelta(hours=1):%H:%M}", f"{best[0]:,}"])
    return title, ["Window (UK)", "Messages"], table


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--teams", action="store_true",
                        help="also post the report to the Teams webhook in TEAMS_WEBHOOK_URL")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="report", required=True)
    f = sub.add_parser("failed", help="top failing iflows", parents=[common])
    f.add_argument("--hours", type=float, default=24)
    f.add_argument("--top", type=int, default=5)
    q = sub.add_parser("quiet", help="quietest 1-hour windows", parents=[common])
    q.add_argument("--days", type=int, default=7)
    q.add_argument("--show", type=int, default=5)
    args = p.parse_args()
    if args.teams and not os.environ.get("TEAMS_WEBHOOK_URL"):
        p.error("--teams needs the TEAMS_WEBHOOK_URL environment variable")

    client = SapMcpClient.from_env()
    client.initialize()
    if args.report == "failed":
        report = report_failed(client, args.hours, args.top)
    else:
        report = report_quiet(client, args.days, args.show)
    if args.teams:
        post_to_teams(os.environ["TEAMS_WEBHOOK_URL"], *report)


if __name__ == "__main__":
    main()
