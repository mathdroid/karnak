"""Pantau MCP server. It exposes the live monitor's data as tools for AI agents.

The server is a cached wrapper over the pantau HTTP API (`/api/summary`). It serves
the Model Context Protocol over streamable HTTP at `/mcp`. It runs as its own process
from the same image. Set PANTAU_API to the pantau service URL.
"""
import os
import time

import httpx
from mcp.server.fastmcp import FastMCP

PANTAU_API = os.getenv("PANTAU_API", "http://pantau:8000").rstrip("/")

mcp = FastMCP(
    "pantau",
    host="0.0.0.0",
    port=int(os.getenv("MCP_PORT", "8000")),
    instructions=(
        "Pantau is a live OSINT monitor for Indonesia. All data comes from public "
        "sources. The tools return the current AI situation brief, the live video "
        "streams that cover the events, the merged news timeline, the reachability "
        "of major platforms from an Indonesian connection, market indicators, and "
        "per-city report counts. All times are WIB (UTC+7)."
    ),
)

_cache = {"ts": 0.0, "data": None}


async def _summary():
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < 15:
        return _cache["data"]
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{PANTAU_API}/api/summary")
        r.raise_for_status()
        data = r.json()
    _cache["data"], _cache["ts"] = data, now
    return data


def _wib(ts):
    return time.strftime("%H:%M", time.gmtime(ts + 7 * 3600)) if ts else ""


@mcp.tool()
async def situation_brief() -> dict:
    """Return the current AI-generated situation brief. The brief lists the most
    significant developments from the live Indonesian news feed. Each development is
    deduplicated and has a time, a place, and links to the cited sources. Call this
    tool first for an overview of the current situation."""
    b = (await _summary()).get("brief") or {}
    return {"updated_wib": _wib(b.get("ts")), "developments": b.get("items", [])}


@mcp.tool()
async def live_streams() -> list:
    """Return the video streams that are live now and cover the events. The sources are
    YouTube news channels, citizen streams, and TikTok live news accounts. Each stream
    is verified as currently live. Each entry has a title, a channel, a concurrent
    viewer count, a platform, and a watch URL."""
    out = []
    for s in (await _summary()).get("streams", []):
        tiktok = s.get("src") == "tiktok"
        out.append({
            "title": s["title"],
            "channel": s.get("channel"),
            "viewers": s.get("viewers"),
            "platform": "tiktok" if tiktok else "youtube",
            "url": (f"https://www.tiktok.com/{s['videoId']}/live" if tiktok
                    else f"https://www.youtube.com/watch?v={s['videoId']}"),
        })
    return out


@mcp.tool()
async def news_timeline(limit: int = 30, critical_only: bool = False, city: str = "") -> list:
    """Return recent merged news items (Google News and publisher RSS), newest first.
    Set critical_only to true to return only high-severity items. High severity covers
    force, arrests, and casualties. Set city, for example 'jakarta' or 'makassar', to
    filter by location. Each entry has a time (WIB), a title, a source, a city, a
    severity, and a url."""
    evs = (await _summary()).get("events", [])
    if critical_only:
        evs = [e for e in evs if e.get("sev") == "high"]
    if city:
        evs = [e for e in evs if (e.get("city") or "").lower() == city.lower()]
    return [{
        "time_wib": _wib(e.get("ts")),
        "title": e.get("title"),
        "source": e.get("publisher") or e.get("source"),
        "city": e.get("city"),
        "severity": e.get("sev"),
        "url": e.get("url"),
    } for e in evs[:max(1, min(limit, 100))]]


@mcp.tool()
async def reachability() -> list:
    """Return the reachability of major platforms from an Indonesian residential
    connection, measured by continuous probes. The status is 'ok' when the platform is
    reachable. The status is 'blocked' when the platform fails only from the Indonesian
    connection. The status is 'down' when the platform fails from both connections.
    Use this tool to detect censorship or throttling."""
    names = {"x.com": "X", "tiktok": "TikTok", "youtube": "YouTube",
             "bluesky": "Bluesky", "telegram": "Telegram"}
    verdicts = (await _summary()).get("verdicts") or {}
    return [{"platform": names.get(k, k), "status": v} for k, v in verdicts.items()]


@mcp.tool()
async def markets() -> list:
    """Return live market indicators: USD/IDR, the Jakarta Composite index (IHSG),
    gold, Brent crude, and Bitcoin. Each entry includes the intraday percentage
    change."""
    m = (await _summary()).get("markets") or {}
    return m.get("items", [])


@mcp.tool()
async def city_activity() -> list:
    """Return the report count per city over the last hour, and the number of those
    reports that are critical."""
    return (await _summary()).get("cityCounts", [])


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
