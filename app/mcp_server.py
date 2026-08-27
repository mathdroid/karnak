"""Pantau MCP server — exposes the live monitor's data as tools for AI agents.

A thin, cached wrapper over the pantau HTTP API (`/api/summary`). Serves the
Model Context Protocol over streamable HTTP at `/mcp`. Runs as its own process
from the same image; set PANTAU_API to the pantau service URL.
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
        "Live OSINT situational monitor for Indonesia. These tools expose, from public "
        "sources: the current AI situation brief, the live video streams covering events, "
        "the fused news timeline, which platforms are reachable from an Indonesian "
        "connection, market indicators, and per-city activity. Times are WIB (UTC+7)."
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
    """The current AI-synthesized situation brief: the few most significant, deduplicated,
    time-stamped developments distilled from the live Indonesian news flood — each with a
    place and links to the cited sources. Start here for 'what is happening right now'."""
    b = (await _summary()).get("brief") or {}
    return {"updated_wib": _wib(b.get("ts")), "developments": b.get("items", [])}


@mcp.tool()
async def live_streams() -> list:
    """Video streams live right now covering events on the ground (YouTube news channels and
    citizen streams, plus TikTok live news accounts). Each is verified currently live.
    Returns title, channel, concurrent viewers, platform, and a watch URL."""
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
    """Recent fused news items (Google News + publisher RSS), newest first. Set critical_only
    to see only high-severity items (force, arrests, casualties); set city (e.g. 'jakarta',
    'makassar') to filter by location. Returns time (WIB), title, source, city, severity, url."""
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
    """Which major platforms are reachable vs blocked from an Indonesian residential
    connection, from continuous probing. status is 'ok' (reachable), 'blocked' (fails only
    from the Indonesian vantage), or 'down'. Use to detect censorship/throttling."""
    names = {"x.com": "X", "tiktok": "TikTok", "youtube": "YouTube",
             "bluesky": "Bluesky", "telegram": "Telegram"}
    verdicts = (await _summary()).get("verdicts") or {}
    return [{"platform": names.get(k, k), "status": v} for k, v in verdicts.items()]


@mcp.tool()
async def markets() -> list:
    """Live market indicators relevant to the situation: USD/IDR, the Jakarta Composite
    (IHSG), gold, Brent crude, and Bitcoin, each with intraday percentage change."""
    m = (await _summary()).get("markets") or {}
    return m.get("items", [])


@mcp.tool()
async def city_activity() -> list:
    """Report volume per city over the last hour, with how many were critical — a quick read
    on where activity is concentrating."""
    return (await _summary()).get("cityCounts", [])


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
