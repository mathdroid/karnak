# Pantau

A single-screen OSINT situational monitor. It fuses public, real-time sources for a
fast-moving civic event into one operations view: a wall of live news streams playing
at once, a synthesized situation brief, a fused news timeline, an activity map, market
indicators, and a platform-reachability (censorship) watch.

Built originally to follow protests in Indonesia, but the sources and keywords are just
configuration — point it at any country or event.

> Everything it shows comes from public sources. No login, no tracking, no accounts.

## What it does

- **Live video wall** — finds every relevant YouTube livestream (search + a channel
  watchlist), verifies each is *actually live*, and plays them simultaneously with
  one-click audio focus.
- **Situation brief** — reads the whole noisy headline flood and an LLM distills the few
  genuinely significant, deduplicated, time-stamped developments, each with a location
  and links back to the sources it used.
- **Fused timeline** — Google News + publisher RSS, deduplicated, tagged by city and
  severity.
- **Activity map** — reports per city over the last hour.
- **Markets** — USD/local-currency, the local index, and macro indicators.
- **Reachability watch** — probes major platforms from a residential vantage vs. a
  direct one to flag throttling/blocking, and logs the transitions.

## Quick start

```bash
git clone <your-fork> pantau && cd pantau
cp .env.example .env      # fill in whatever keys you have (all optional)
docker compose up -d --build
open http://localhost:8000
```

That's it. **With no keys at all** you still get the YouTube live wall and the Google
News timeline. Each key you add lights up one more collector.

## Keys (all optional)

| Feature | Env var | Where to get it |
|---|---|---|
| Situation brief | `OPENROUTER_API_KEY` | <https://openrouter.ai> — any chat model via `SUMMARY_MODEL` |
| TikTok LIVE detection | `SIGN_API_KEY` | <https://www.eulerstream.com> (TikTokLive signing) |
| TikTok + markets egress | `PROXY` | any residential SOCKS5 proxy you control |

**Why a proxy?** TikTok and Yahoo Finance block datacenter/cloud IPs. Those two
collectors route through `PROXY` (a `socks5://host:port` on a residential line). Leave it
blank and they're simply skipped — nothing else is affected. A Tailscale exit node on a
home connection, or any commercial residential proxy, works.

## Configure it for your event

Everything region-specific lives at the top of `app/main.py` — edit and rebuild:

- `YT_SEARCH_QUERIES`, `GNEWS_QUERIES` — the keywords to track
- `YT_CHANNELS` — news channels to check for a live stream (name, channel id, handle)
- `TIKTOK_WATCH` — TikTok handles to watch for going live
- `MEDIA_FEEDS` — publisher RSS feeds
- `CITIES` — place names → map coordinates (in `app/static/index.html`)
- `MARKET_SYMBOLS` — Yahoo Finance tickers
- `SEV_HIGH` / `SEV_MED` — the words that mark an item critical

## Architecture

A single FastAPI service. Async collectors poll each source on their own cadence and
write normalized rows to SQLite (`data/pantau.db`); the frontend is one static page that
polls `/api/summary` every 30s. No build step, no framework — just `main.py` and one
`index.html`.

```
collectors ──> SQLite ──> /api/summary ──> index.html
```

## Deploying behind a domain

The container serves plain HTTP on `:8000` — front it with whatever you already run.
`compose.yaml` includes commented Traefik labels; set `DOMAIN` in `.env` and uncomment
them, or put it behind nginx/Caddy/Cloudflare Tunnel yourself.

## License

MIT — see [LICENSE](LICENSE).
