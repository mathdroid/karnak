"""PANTAU — protest OSINT fusion service.

Collectors poll public sources server-side, normalize into SQLite, and the
frontend polls /api/summary. Blocked-from-datacenter sources (Waze API,
Reddit) are excluded; Bluesky is fetched client-side (open CORS, clean IP).
Censorship probes run through a residential exit (Indonesian ISP) against the
server's own datacenter path as the control.
"""
import asyncio
import calendar
import hashlib
import json
import os
import re
import sqlite3
import time

import feedparser
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = "/data/pantau.db"
# Residential SOCKS proxy for sources that block datacenter IPs (TikTok, Yahoo markets).
# Point it at your own proxy; if unset those two collectors are skipped entirely.
PROXY = os.getenv("PROXY", "")
SITE_URL = os.getenv("SITE_URL", "https://pantau.example")  # sent to OpenRouter as referer
UA_HEADERS = {"Accept-Language": "en-US,en;q=0.9,id;q=0.8"}

YT_SEARCH_QUERIES = [
    "demo dpr", "demo jakarta", "aksi 27 agustus", "demo indonesia",
    "demo mahasiswa", "demo buruh", "demo makassar", "demo medan",
    "demo surabaya", "demo semarang", "demo bandung", "demo yogyakarta",
    "demo papua",
]
YT_CHANNELS = [
    ("Kompas TV", "UC5BMIWZe9isJXLZZWPWvBlg", "KompasTV"),
    ("tvOne News", "UCER4rvDnRBPr_ncYW4UCZjg", "tvOneNews"),
    ("CNN Indonesia", "UCKII0Ml9S5wneKbHswmUrIQ", "CNNIDOFFICIAL"),
    ("MetroTV", "UCkbPntO_8G2BF2HmLcrsZXA", "metrotv"),
    ("BeritaSatu", "UCqLsfkQSM0yfyGvONAGWd3Q", "BeritaSatuChannel"),
    ("Narasi Newsroom", "UCnOf30K0d0e8M7mc4FUzR0A", "NarasiNewsroom"),
    ("Tempodotco", "UC3QRoNY-nYDTNSv-1dR0P-g", "tempovideochannel"),
    ("Jakartanicus", "UCSu9irj71BQuaoC5V7-s_YA", "Jakartanicus"),
]
GNEWS_QUERIES = [
    "demo DPR", "demo Jakarta", "aksi 27 agustus", "demo mahasiswa",
    "demo buruh", "kerusuhan demo", "gas air mata demo", "demo Makassar",
    "demo Medan", "demo Surabaya", "demo Semarang", "demo Papua",
]
MEDIA_FEEDS = [
    ("tempo", "https://rss.tempo.co/nasional"),
    ("cnn-id", "https://www.cnnindonesia.com/nasional/rss"),
    ("bbc-id", "https://feeds.bbci.co.uk/indonesia/rss.xml"),
    ("antara", "https://www.antaranews.com/rss/politik.xml"),
]
# TikTok LIVE watchlist — validated live-capable handles (is_live via EulerStream
# signing, routed through the NAS residential exit since TikTok blocks the VPS IP).
# TikTok has no keyless keyword discovery and lives can't be embedded (X-Frame-Options),
# so we detect known outlets going live and link out to them.
SIGN_API_KEY = os.getenv("SIGN_API_KEY", "")
# AI summarizer — droid/stellie OpenRouter key. "ox-alpha" is not a real OpenRouter
# model (404), so we use GLM Flash as offered; free GLM is the no-credit fallback.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Set BRIEF_PAUSED=1 to stop brief generation (and its LLM cost) without
# removing the panel; the page keeps the last brief and reports the pause.
BRIEF_PAUSED = os.getenv("BRIEF_PAUSED", "").lower() in ("1", "true", "yes")
# Set CHAT_ENABLED=1 when a chat backend is routed at /chat/* on the same
# origin (see README); the frontend renders the Chat tab only when true.
CHAT_ENABLED = os.getenv("CHAT_ENABLED", "").lower() in ("1", "true", "yes")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "z-ai/glm-5.3-flash")
SUMMARY_FALLBACK = os.getenv("SUMMARY_FALLBACK", "z-ai/glm-5.2:free")
BRIEF_PROMPT = (
    "You are an OSINT analyst monitoring protests in Indonesia. Below are Indonesian news "
    "headlines, each prefixed with [index] and the time it was PUBLISHED (WIB). The feed is "
    "noisy and repetitive. Extract the 6-8 MOST SIGNIFICANT DISTINCT developments on the "
    "ground: escalations, arrests (with numbers), injuries/casualties, use of force (tear gas, "
    "water cannon), road/building blockades, crowd sizes and movements, major official actions. "
    "DEDUPE repeated coverage of the same event into ONE line. IGNORE routine reassurances, "
    "opinion, and generic 'expect traffic' notices. For each development return: "
    "'time' = when the EVENT itself happened in WIB 'HH:MM' (infer from the wording; only if "
    "there is no cue use the publication time), 'place' = the specific location as 'venue, city' "
    "(e.g. 'DPR, Jakarta' or 'Tugu Muda, Semarang'), 'text' = one concrete factual English line "
    "(<=22 words, keep numbers, active voice, plain literal wording, no metaphors, no em dashes), "
    "'sources' = array of the [index] numbers you drew from. "
    "ORDER NEWEST FIRST by time. Return ONLY a JSON array of "
    '{"time","place","text","sources"}.'
)
# Market indicators — Yahoo Finance via the NAS residential exit (datacenter IP is 429'd
# and needs a cookie+crumb handshake). invert=True colours by rupiah strength, so a rising
# USD/IDR (weaker rupiah) shows red.
MARKET_SYMBOLS = [
    ("IDR=X", "USD/IDR", True),
    ("^JKSE", "IHSG", False),
    ("GC=F", "Gold", False),
    ("BZ=F", "Brent", False),
    ("BTC-USD", "BTC", False),
]
TIKTOK_WATCH = [
    "@tvonenews", "@detikcom", "@cnnindonesia", "@tribunnews", "@liputan6",
    "@kumparan", "@idntimes", "@cnbcindonesia", "@antaranews", "@sindonews",
    "@kompascom", "@tempo.co", "@jpnncom", "@viva.co.id", "@republikaonline",
    "@bbcnewsindonesia",
]
MEDIA_FILTER = re.compile(
    r"demo|unjuk rasa|aksi massa|massa aksi|ricuh|kerusuhan|gas air mata|"
    r"blokade|long ?march|mahasiswa|buruh turun|dpr|represif|penangkapan|"
    r"tuntutan rakyat|merebut kemerdekaan", re.I)
STREAM_KEEP = re.compile(
    r"demo|aksi|unjuk|massa|dpr|mahasiswa|buruh|ricuh|kawal|protes|"
    r"long ?march|patung kuda|monas|bundaran|24 jam", re.I)
STREAM_SPAM = re.compile(
    r"trading|forex|xau|gold|crypto|bitcoin|binomo|olymp|pocket option|"
    r"slot|gacor|zeus|rtp|judi|giveaway|sinyal|scalping|akun demo|"
    r"demo account|demo trading|saham|\bdj\b|lagu|musi[ck]|karaoke|radio|"
    r"honor of kings|mobile legend|free fire|gameplay|game ?play|esports|"
    r"unboxing|giveaway|tutorial", re.I)
SEV_HIGH = re.compile(
    r"ricuh|rusuh|bentrok|gas air mata|tembak|korban|tewas|meninggal|luka|"
    r"ditangkap|penangkapan|represif|blokir|throttl|kerusuhan|dibakar|"
    r"terbakar|anarkis|water cannon|peluru", re.I)
SEV_MED = re.compile(
    r"tutup jalan|penutupan|blokade|macet total|bubarkan|dipukul mundur|"
    r"bertahan|dorong-dorongan|barikade", re.I)
CITIES = {
    "jakarta": ["jakarta", "dpr", "senayan", "bundaran hi", "monas", "patung kuda", "istana"],
    "makassar": ["makassar"], "medan": ["medan"], "surabaya": ["surabaya"],
    "semarang": ["semarang"], "bandung": ["bandung"],
    "yogyakarta": ["yogyakarta", "jogja"], "palembang": ["palembang"],
    "pati": ["pati"], "jayapura": ["jayapura"], "sorong": ["sorong"],
    "wamena": ["wamena"], "nabire": ["nabire"], "aceh": ["aceh"],
    "denpasar": ["denpasar", "bali"], "papua": ["papua"],
}
PROBE_TARGETS = [
    ("x.com", "https://x.com/"),
    ("tiktok", "https://www.tiktok.com/"),
    ("youtube", "https://www.youtube.com/"),
    ("bluesky", "https://bsky.app/"),
    ("telegram", "https://t.me/"),
    ("control", "https://www.google.com/generate_204"),
]

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db_lock = asyncio.Lock()
db.executescript("""
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, ts INTEGER, source TEXT, title TEXT, url TEXT,
  publisher TEXT, city TEXT, sev TEXT, first_seen INTEGER);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE TABLE IF NOT EXISTS streams (
  video_id TEXT PRIMARY KEY, title TEXT, channel TEXT, viewers INTEGER,
  live INTEGER, src TEXT, first_seen INTEGER, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS probes (
  ts INTEGER, target TEXT, vantage TEXT, ok INTEGER, code INTEGER, ms INTEGER);
CREATE INDEX IF NOT EXISTS pr_ts ON probes(ts);
CREATE TABLE IF NOT EXISTS source_status (
  source TEXT PRIMARY KEY, last_ok INTEGER, last_err TEXT, items INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT, ts INTEGER);
""")
try:
    db.execute("ALTER TABLE events ADD COLUMN summary TEXT")
except sqlite3.OperationalError:
    pass  # column already exists
db.commit()


def classify(text):
    low = text.lower()
    city = next((c for c, aliases in CITIES.items()
                 if any(a in low for a in aliases)), None)
    sev = "high" if SEV_HIGH.search(low) else ("med" if SEV_MED.search(low) else "info")
    return city, sev


# /api/summary is public, and status errors surface in the footer. Strip anything that
# could leak infra: API keys, proxy URLs, and IP/host addresses (incl. tailnet IPs).
_REDACT = re.compile(
    r"sk-or-v1-[A-Za-z0-9_-]+|euler_[A-Za-z0-9_-]+|socks5?://\S+|hostrelay"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"          # IPv4 (+port)
    r"|\b[A-Fa-f0-9]{0,4}(?::[A-Fa-f0-9]{0,4}){2,}\b", re.I)  # IPv6 (incl. ::)


def sanitize_err(msg):
    return _REDACT.sub("[redacted]", msg or "")


async def record_status(source, ok, note, items=0):
    async with db_lock:
        if ok:
            db.execute(
                "INSERT INTO source_status VALUES(?,?,?,?) ON CONFLICT(source) "
                "DO UPDATE SET last_ok=excluded.last_ok, last_err='', items=excluded.items",
                (source, int(time.time()), "", items))
        else:
            db.execute(
                "INSERT INTO source_status VALUES(?,0,?,0) ON CONFLICT(source) "
                "DO UPDATE SET last_err=excluded.last_err",
                (source, sanitize_err(note)[:200]))
        db.commit()


async def add_events(rows):
    """rows: (id, ts, source, title, url, publisher). Returns # new."""
    now, new = int(time.time()), 0
    async with db_lock:
        for eid, ts, source, title, url, publisher in rows:
            city, sev = classify(title)
            cur = db.execute(
                "INSERT OR IGNORE INTO events "
                "(id,ts,source,title,url,publisher,city,sev,first_seen) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (eid, ts, source, title.strip(), url, publisher, city, sev, now))
            new += cur.rowcount
        db.commit()
    return new


def eid_for(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def parse_rss_entries(text, source, publisher_from_entry=False):
    rows = []
    for e in feedparser.parse(text).entries[:40]:
        title = getattr(e, "title", "")
        link = getattr(e, "link", "")
        if not title or not link:
            continue
        ts = int(time.time())
        for attr in ("published_parsed", "updated_parsed"):
            parsed = getattr(e, attr, None)
            if parsed:
                ts = calendar.timegm(parsed)
                break
        publisher = source
        if publisher_from_entry and getattr(e, "source", None):
            publisher = getattr(e.source, "title", source)
        rows.append((eid_for(source, link), ts, source, title, link, publisher))
    return rows


# ---------------------------------------------------------------- collectors

def walk_video_renderers(node, out):
    if isinstance(node, dict):
        if "videoRenderer" in node:
            out.append(node["videoRenderer"])
        for v in node.values():
            walk_video_renderers(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_video_renderers(v, out)


def renderer_is_live(v):
    for o in v.get("thumbnailOverlays", []):
        style = o.get("thumbnailOverlayTimeStatusRenderer", {}).get("style")
        if style == "LIVE":
            return True
    return any(b.get("metadataBadgeRenderer", {}).get("style") == "BADGE_STYLE_TYPE_LIVE_NOW"
               for b in v.get("badges", []))


def renderer_viewers(v):
    for key in ("viewCountText", "shortViewCountText"):
        runs = v.get(key, {}).get("runs", [])
        if runs:
            digits = re.sub(r"[^\d]", "", runs[0].get("text", ""))
            if digits:
                return int(digits)
    return 0


async def verify_live(session, video_ids):
    """Fetch each watch page; return {vid: concurrent_viewers} for those live NOW.

    The search-result LIVE badge is not authoritative — a stream that just ended
    keeps the badge briefly and lingers. "isLiveNow":true on the watch page is the
    ground truth, and originalViewCount there is the concurrent count while live.
    """
    sem = asyncio.Semaphore(8)
    live = {}

    async def check(vid):
        async with sem:
            try:
                r = await session.get(f"https://www.youtube.com/watch?v={vid}",
                                      headers=UA_HEADERS)
                if '"isLiveNow":true' in r.text:
                    m = re.search(r'"originalViewCount":"(\d+)"', r.text)
                    live[vid] = int(m.group(1)) if m else 0
            except Exception:
                pass

    await asyncio.gather(*(check(v) for v in set(video_ids)))
    return live


async def upsert_streams(found, src, authoritative=False):
    """found: {video_id: (title, channel, viewers)}.

    authoritative=True: `found` is the verified-live set this cycle, so any other
    live stream of this src is now dead — drop it immediately (no 6-min lag).
    authoritative=False: fall back to a time-based prune (used for stable channels).
    """
    now = int(time.time())
    async with db_lock:
        for vid, (title, channel, viewers) in found.items():
            db.execute(
                "INSERT INTO streams VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(video_id) DO UPDATE SET "
                "title=excluded.title, viewers=excluded.viewers, live=1, last_seen=excluded.last_seen",
                (vid, title, channel, viewers, src, now, now))
        if authoritative and found:
            keep = ",".join("?" * len(found))
            db.execute(f"UPDATE streams SET live=0 WHERE src=? AND video_id NOT IN ({keep})",
                       (src, *found.keys()))
        else:
            db.execute("UPDATE streams SET live=0 WHERE src=? AND last_seen < ?",
                       (src, now - 360))
        db.commit()


async def collect_yt_search(session):
    found = {}
    for q in YT_SEARCH_QUERIES:
        url = ("https://www.youtube.com/results?search_query="
               + q.replace(" ", "+") + "&sp=EgJAAQ%253D%253D")
        r = await session.get(url, headers=UA_HEADERS)
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text, re.S)
        if not m:
            continue
        renderers = []
        walk_video_renderers(json.loads(m.group(1)), renderers)
        for v in renderers:
            if not renderer_is_live(v):
                continue
            vid = v.get("videoId")
            title = "".join(run.get("text", "") for run in v.get("title", {}).get("runs", []))
            channel = "".join(run.get("text", "") for run in
                              v.get("ownerText", {}).get("runs", []))
            if (vid and title and STREAM_KEEP.search(title)
                    and not STREAM_SPAM.search(title)):
                found[vid] = (title, channel, renderer_viewers(v))
        await asyncio.sleep(1.2)

    if not found:  # search blip — don't tear down the live set on an empty cycle
        await record_status("yt-search", True, "", 0)
        return

    # Re-verify streams we currently show too, so one that ended between cycles
    # (and thus isn't in `found`) is confirmed dead rather than assumed dead.
    async with db_lock:
        prior = [r[0] for r in db.execute(
            "SELECT video_id FROM streams WHERE src='search' AND live=1").fetchall()]
    live_now = await verify_live(session, list(found) + prior)
    verified = {vid: found.get(vid, (
        # kept-alive prior stream not re-found this cycle: reuse its stored row
        None, None, 0))[:2] + (live_now[vid],)
        for vid in live_now}
    # Fill title/channel for prior-only streams from the DB.
    missing = [v for v in verified if verified[v][0] is None]
    if missing:
        async with db_lock:
            rows = db.execute(
                f"SELECT video_id,title,channel FROM streams WHERE video_id IN "
                f"({','.join('?' * len(missing))})", missing).fetchall()
        meta = {vid: (t, c) for vid, t, c in rows}
        for v in missing:
            t, c = meta.get(v, ("", ""))
            verified[v] = (t, c, verified[v][2])

    await upsert_streams(verified, "search", authoritative=True)
    await record_status("yt-search", True, "", len(verified))


async def collect_yt_channels(session):
    cand = {}  # vid -> (title, channel_name)
    for name, _cid, handle in YT_CHANNELS:
        try:
            r = await session.get(f"https://www.youtube.com/@{handle}/live",
                                  headers=UA_HEADERS)
            m = re.search(r'rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]+)"',
                          r.text)
            if m:
                tm = re.search(r"<title>([^<]*)</title>", r.text)
                cand[m.group(1)] = (
                    tm.group(1).replace(" - YouTube", "") if tm else name, name)
        except Exception:
            continue
        await asyncio.sleep(1)
    # /live can resolve to an ended or upcoming video; confirm isLiveNow before trusting it.
    live_now = await verify_live(session, list(cand))
    found = {vid: (cand[vid][0], cand[vid][1], live_now[vid]) for vid in live_now}
    await upsert_streams(found, "channel", authoritative=True)
    await record_status("yt-channels", True, "", len(found))


async def collect_gnews(session):
    total = 0
    for q in GNEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + q.replace(" ", "%20")
               + "%20when:1d&hl=id&gl=ID&ceid=ID:id")
        r = await session.get(url)
        rows = [row for row in parse_rss_entries(r.text, "news", publisher_from_entry=True)
                if MEDIA_FILTER.search(row[3])]
        total += await add_events(rows)
        await asyncio.sleep(1)
    await record_status("gnews", True, "", total)


async def collect_media_rss(session):
    total = 0
    for name, url in MEDIA_FEEDS:
        try:
            r = await session.get(url)
            rows = [row for row in parse_rss_entries(r.text, name)
                    if MEDIA_FILTER.search(row[3])]
            total += await add_events(rows)
        except Exception:
            continue
    await record_status("media-rss", True, "", total)


async def collect_tiktok():
    """Poll the watchlist sequentially through the residential exit; surface any live.

    TikTokLive's is_live() hits TikTok's web directly, which blocks this VPS's
    datacenter IP, so every call routes through the NAS SOCKS proxy. Sequential with
    a gap — bursting trips TikTok's anti-bot (HTML instead of JSON).
    """
    from TikTokLive import TikTokLiveClient
    try:
        from TikTokLive.client.web.web_settings import WebDefaults
        if SIGN_API_KEY:
            WebDefaults.tiktok_sign_api_key = SIGN_API_KEY
    except Exception:
        pass

    found = {}
    for handle in TIKTOK_WATCH:
        try:
            client = TikTokLiveClient(unique_id=handle, web_proxy=PROXY)
            if await client.is_live():
                name = handle.lstrip("@")
                found[handle] = (f"{name} is live on TikTok", "TikTok", 0)
        except Exception:
            pass  # UserNotFound / transient proxy error — skip this handle this cycle
        await asyncio.sleep(2)
    await upsert_streams(found, "tiktok", authoritative=bool(found))
    await record_status("tiktok", True, "", len(found))


def compute_verdicts(probe_map):
    """Map raw probe results per target to ok / blocked / down / vantage-down."""
    verdicts = {}
    ctrl = probe_map.get("control", {})
    ctrl_id_ok = ctrl.get("id-residential", {}).get("ok", False)
    for target, v in probe_map.items():
        if target == "control":
            continue
        id_ok = v.get("id-residential", {}).get("ok")
        direct_ok = v.get("direct", {}).get("ok")
        if id_ok is None:
            verdicts[target] = "unknown"
        elif id_ok:
            verdicts[target] = "ok"
        elif direct_ok and ctrl_id_ok:
            verdicts[target] = "blocked"       # fails only from the ID vantage
        elif not ctrl_id_ok:
            verdicts[target] = "vantage-down"  # our probe path itself is broken
        else:
            verdicts[target] = "down"
    return verdicts


async def latest_probe_map():
    async with db_lock:
        rows = db.execute("SELECT target,vantage,ok,code,ms,max(ts) FROM probes "
                          "GROUP BY target,vantage").fetchall()
    pm = {}
    for target, vantage, ok, code, ms, ts in rows:
        pm.setdefault(target, {})[vantage] = {"ok": bool(ok), "code": code, "ms": ms, "ts": ts}
    return pm


PLATFORM_NAMES = {"x.com": "X", "tiktok": "TikTok", "youtube": "YouTube",
                  "bluesky": "Bluesky", "telegram": "Telegram"}
_last_verdicts = {}


async def emit_connectivity_events(verdicts):
    """Turn probe verdict *transitions* into timeline events — this is our live
    censorship signal now that the NetBlocks feed is defunct."""
    now = int(time.time())
    rows = []
    for target, v in verdicts.items():
        prev = _last_verdicts.get(target)
        if prev is not None and prev != v:
            name = PLATFORM_NAMES.get(target, target)
            if v in ("blocked", "down"):
                title = (f"{name} unreachable from Indonesian residential network"
                         if v == "blocked" else f"{name} appears down")
            elif v == "ok" and prev in ("blocked", "down"):
                title = f"{name} reachable again from Indonesian network"
            else:
                _last_verdicts[target] = v
                continue
            rows.append((eid_for("conn", target, str(now)), now, "connectivity",
                         title, "https://radar.cloudflare.com/id", "Probe"))
        _last_verdicts[target] = v
    if rows:
        await add_events(rows)
        async with db_lock:
            db.execute("UPDATE events SET sev='high' WHERE source='connectivity' "
                       "AND (title LIKE '%unreachable%' OR title LIKE '%down%')")
            db.commit()


async def probe_once(session, name, url, vantage):
    t0 = time.time()
    try:
        r = await session.get(url, headers=UA_HEADERS, timeout=12,
                              allow_redirects=False)
        ok, code = int(r.status_code < 500), r.status_code
    except Exception:
        ok, code = 0, 0
    ms = int((time.time() - t0) * 1000)
    async with db_lock:
        db.execute("INSERT INTO probes VALUES(?,?,?,?,?,?)",
                   (int(time.time()), name, vantage, ok, code, ms))
        db.commit()


async def collect_probes(direct, viaproxy):
    for name, url in PROBE_TARGETS:
        await probe_once(viaproxy, name, url, "id-residential")
        await probe_once(direct, name, url, "direct")
    await emit_connectivity_events(compute_verdicts(await latest_probe_map()))
    await record_status("probes", True, "", len(PROBE_TARGETS))


async def _openrouter_json(session, prompt, status_name, max_tokens=2600):
    """Call the summary model (paid) then the free fallback; return the parsed JSON
    array from the reply, or None. GLM Flash forces a hidden reasoning pass, so the
    token budget must cover reasoning + output."""
    for model in (SUMMARY_MODEL, SUMMARY_FALLBACK):
        try:
            r = await session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json",
                         "HTTP-Referer": SITE_URL,
                         "X-Title": "pantau"},
                # Cap the (mandatory) reasoning — uncapped it runs 80s+ and overruns
                # max_tokens, leaving content empty. Capped it returns clean JSON in ~7s.
                json={"model": model, "temperature": 0.3, "max_tokens": max_tokens,
                      "reasoning": {"max_tokens": 2000},
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=90)
            data = r.json()
            if "choices" not in data:
                msg = str(data.get("error", {}).get("message", data))[:90]
                await record_status(status_name, False, f"{model}: {msg}")
                continue  # capped/rate-limited → try fallback model
            content = data["choices"][0]["message"].get("content") or ""
            m = re.search(r"\[.*\]", content, re.S)
            if m:
                try:
                    arr = json.loads(m.group(0))
                    if isinstance(arr, list):
                        return arr
                except Exception:
                    pass
            await record_status(status_name, False, f"{model}: unparseable")
        except Exception as exc:
            await record_status(status_name, False, f"{model}: {type(exc).__name__}")
    return None


async def generate_brief(session):
    """The needle in the haystack: read the whole recent headline flood and synthesize
    the few genuinely significant developments — each with event time, place, and links
    back to the cited items."""
    if BRIEF_PAUSED:
        await record_status("brief", True, "", 0)
        return
    if not OPENROUTER_API_KEY:
        await record_status("brief", False, "no OPENROUTER_API_KEY")
        return
    now = int(time.time())
    async with db_lock:
        rows = db.execute(
            "SELECT ts,title,url FROM events WHERE ts > ? AND source != 'connectivity' "
            "ORDER BY ts DESC LIMIT 55", (now - 6 * 3600,)).fetchall()
    if len(rows) < 4:
        await record_status("brief", True, "", 0)
        return
    idx_url = {}
    lines = []
    for i, (ts, title, url) in enumerate(rows):
        idx_url[i] = url
        lines.append(f"[{i}] {time.strftime('%H:%M', time.gmtime(ts + 7 * 3600))} {title}")
    arr = await _openrouter_json(session, BRIEF_PROMPT + "\n\nHEADLINES:\n" + "\n".join(lines),
                                 "brief", max_tokens=4500)  # GLM reasoning needs headroom
    if arr is None:
        return  # error already recorded; keep the previous brief, retry next cycle
    items = []
    for x in arr:
        if not isinstance(x, dict) or not x.get("text"):
            continue
        urls = []
        for si in (x.get("sources") or []):
            try:
                u = idx_url.get(int(si))
            except (ValueError, TypeError):
                u = None
            if u and u not in urls:
                urls.append(u)
        items.append({"time": str(x.get("time", ""))[:5], "place": str(x.get("place", ""))[:60],
                      "text": str(x.get("text", ""))[:220], "urls": urls[:3]})
    items.sort(key=lambda z: z["time"], reverse=True)   # newest first
    items = items[:8]
    async with db_lock:
        db.execute("INSERT INTO meta VALUES('brief',?,?) ON CONFLICT(key) DO UPDATE SET "
                   "value=excluded.value, ts=excluded.ts", (json.dumps(items), now))
        db.commit()
    await record_status("brief", True, "", len(items))


async def collect_markets(viaproxy):
    """USD/IDR, IHSG and macro indicators from Yahoo via the residential exit."""
    try:
        await viaproxy.get("https://fc.yahoo.com/")  # seed cookies
        crumb = (await viaproxy.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb")).text.strip()
        r = await viaproxy.get("https://query1.finance.yahoo.com/v7/finance/quote",
                               params={"symbols": ",".join(s for s, _l, _i in MARKET_SYMBOLS),
                                       "crumb": crumb})
        result = r.json()["quoteResponse"]["result"]
    except Exception as exc:
        await record_status("markets", False, f"{type(exc).__name__}: {exc}")
        return
    bysym = {q.get("symbol"): q for q in result}
    out = []
    for sym, label, invert in MARKET_SYMBOLS:
        q = bysym.get(sym)
        if not q or q.get("regularMarketPrice") is None:
            continue
        chg = q.get("regularMarketChangePercent") or 0.0
        out.append({"label": label, "price": q["regularMarketPrice"], "chg": round(chg, 2),
                    "up": (chg < 0) if invert else (chg > 0)})
    if out:
        async with db_lock:
            db.execute("INSERT INTO meta VALUES('markets',?,?) ON CONFLICT(key) DO UPDATE SET "
                       "value=excluded.value, ts=excluded.ts", (json.dumps(out), int(time.time())))
            db.commit()
    await record_status("markets", True, "", len(out))


async def prune_loop():
    while True:
        cutoff = int(time.time())
        async with db_lock:
            db.execute("DELETE FROM events WHERE ts < ?", (cutoff - 48 * 3600,))
            db.execute("DELETE FROM probes WHERE ts < ?", (cutoff - 24 * 3600,))
            db.execute("DELETE FROM streams WHERE last_seen < ?", (cutoff - 24 * 3600,))
            db.commit()
        await asyncio.sleep(3600)


async def run_every(interval, fn, *args, name=""):
    while True:
        try:
            await fn(*args)
        except Exception as exc:  # keep the loop alive; surface in status API
            await record_status(name or fn.__name__, False, f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(interval)


app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=1000)
tasks = []


@app.on_event("startup")
async def startup():
    direct = AsyncSession(impersonate="chrome124", timeout=25)
    # The residential vantage needs a proxy; without one it falls back to direct
    # (reachability still runs, but can't distinguish a region-specific block).
    viaproxy = (AsyncSession(impersonate="chrome124", timeout=25,
                             proxies={"http": PROXY, "https": PROXY},
                             curl_options={CurlOpt.IPRESOLVE: 1})
                if PROXY else direct)
    tasks.extend([
        asyncio.create_task(run_every(90, collect_yt_search, direct, name="yt-search")),
        asyncio.create_task(run_every(120, collect_yt_channels, direct, name="yt-channels")),
        asyncio.create_task(run_every(150, collect_gnews, direct, name="gnews")),
        asyncio.create_task(run_every(300, collect_media_rss, direct, name="media-rss")),
        asyncio.create_task(run_every(240, collect_probes, direct, viaproxy, name="probes")),
        asyncio.create_task(run_every(300, generate_brief, direct, name="brief")),
        asyncio.create_task(prune_loop()),
    ])
    # These sources block datacenter IPs, so they only run when a residential PROXY is set.
    if PROXY and SIGN_API_KEY:
        tasks.append(asyncio.create_task(run_every(210, collect_tiktok, name="tiktok")))
    if PROXY:
        tasks.append(asyncio.create_task(run_every(120, collect_markets, viaproxy, name="markets")))


@app.get("/api/summary")
async def summary():
    now = int(time.time())
    async with db_lock:
        streams = db.execute(
            "SELECT video_id,title,channel,viewers,src,first_seen,last_seen "
            "FROM streams WHERE live=1 ORDER BY (src='tiktok') DESC, viewers DESC").fetchall()
        events = db.execute(
            "SELECT id,ts,source,title,url,publisher,city,sev,summary FROM events "
            "WHERE ts > ? ORDER BY ts DESC LIMIT 250", (now - 24 * 3600,)).fetchall()
        probes = db.execute(
            "SELECT target,vantage,ok,code,ms,max(ts) FROM probes "
            "GROUP BY target,vantage").fetchall()
        status = db.execute(
            "SELECT source,last_ok,last_err,items FROM source_status").fetchall()
        city_counts = db.execute(
            "SELECT city,count(*),sum(sev='high') FROM events "
            "WHERE ts > ? AND city IS NOT NULL GROUP BY city", (now - 3600,)).fetchall()
        brief_row = db.execute("SELECT value,ts FROM meta WHERE key='brief'").fetchone()
        markets_row = db.execute("SELECT value,ts FROM meta WHERE key='markets'").fetchone()

    probe_map = {}
    for target, vantage, ok, code, ms, ts in probes:
        probe_map.setdefault(target, {})[vantage] = {
            "ok": bool(ok), "code": code, "ms": ms, "ts": ts}
    verdicts = compute_verdicts(probe_map)
    return {
        "now": now,
        "streams": [dict(zip(
            ("videoId", "title", "channel", "viewers", "src", "firstSeen", "lastSeen"), s))
            for s in streams],
        "events": [dict(zip(
            ("id", "ts", "source", "title", "url", "publisher", "city", "sev", "summary"), e))
            for e in events],
        "probes": probe_map, "verdicts": verdicts,
        "sources": [dict(zip(("source", "lastOk", "lastErr", "items"), s)) for s in status],
        "cityCounts": [dict(zip(("city", "count", "high"), c)) for c in city_counts],
        "brief": {"items": json.loads(brief_row[0]), "ts": brief_row[1]} if brief_row else None,
        "briefPaused": BRIEF_PAUSED,
        "chatEnabled": CHAT_ENABLED,
        "markets": {"items": json.loads(markets_row[0]), "ts": markets_row[1]} if markets_row else None,
    }


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
