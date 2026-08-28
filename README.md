# Pantau

Pantau is a single-screen web dashboard that monitors a fast-moving civic event through
public sources. It combines six views on one page: a grid of live news video streams, an
AI-generated situation brief, a merged news timeline, an activity map, market indicators,
and a platform reachability panel.

"Pantau" is Indonesian for "to watch". The repository codename is "Karnak", after the
fortress of Ozymandias and its
[wall of monitors](https://tvobsessive.com/wp-content/uploads/2019/09/ozymandias-veidt-cat-screens-featured-e1569021951280-758x412.jpg)
in *Watchmen* (© DC Comics / HBO).

The default configuration follows protests in Indonesia. The sources and keywords are
configuration values, so you can point the monitor at any country or event.

All displayed data comes from public sources. The site requires no login and no account.
The site does not track visitors.

## Features

- **Live video wall.** The collector finds relevant YouTube livestreams through search
  and a channel watchlist. It verifies that each stream is live at that moment. The page
  plays all streams at the same time, and one click moves the audio focus to one stream.
- **Situation brief.** An LLM reads the collected headlines and returns the most
  significant developments. The LLM deduplicates the developments and stamps each one
  with a time, a location, and links to its sources.
- **Timeline.** The timeline merges Google News and publisher RSS feeds. The collector
  deduplicates the items and tags each item with a city and a severity.
- **Activity map.** The map shows the number of reports per city over the last hour.
- **Markets.** The panel shows the US dollar to local currency rate, the local stock
  index, and macro indicators.
- **Reachability panel.** The server probes major platforms from a residential
  connection and from a direct datacenter connection. A platform that fails only from
  the residential connection is marked as blocked. The monitor logs each status
  transition.

## Quick start

```bash
git clone <your-fork> pantau && cd pantau
cp .env.example .env      # fill in whatever keys you have (all optional)
docker compose up -d --build
open http://localhost:8000
```

The monitor runs without any API keys. In that mode it serves the YouTube live wall and
the Google News timeline. Each additional key enables one more collector.

## API keys

Every key is optional.

| Feature | Env var | Source |
|---|---|---|
| Situation brief | `OPENROUTER_API_KEY` | <https://openrouter.ai>. Select any chat model with `SUMMARY_MODEL`. |
| TikTok LIVE detection | `SIGN_API_KEY` | <https://www.eulerstream.com> (TikTokLive signing). |
| TikTok and markets egress | `PROXY` | Any residential SOCKS5 proxy that you control. |

Set `BRIEF_PAUSED=1` to pause brief generation and its LLM cost during a quiet period.
The page keeps the last brief and reports that updates are paused.

## Proxy requirement

TikTok and Yahoo Finance block requests from datacenter IP addresses. The TikTok
collector and the markets collector therefore send their requests through `PROXY`.
`PROXY` is a `socks5://host:port` address on a residential connection. If you leave
`PROXY` blank, the app skips these two collectors, and the other collectors are not
affected. A Tailscale exit node on a home connection works. A commercial residential
proxy also works.

## Configuration

All region-specific values are defined at the top of `app/main.py`. Edit the values and
rebuild the image.

| Value | Contents |
|---|---|
| `YT_SEARCH_QUERIES`, `GNEWS_QUERIES` | The keywords to track. |
| `YT_CHANNELS` | News channels to check for a live stream. Each entry has a name, a channel id, and a handle. |
| `TIKTOK_WATCH` | TikTok handles to check for live status. |
| `MEDIA_FEEDS` | Publisher RSS feeds. |
| `CITIES` | Place names and their map coordinates. This value is in `app/static/index.html`. |
| `MARKET_SYMBOLS` | Yahoo Finance tickers. |
| `SEV_HIGH`, `SEV_MED` | The words that mark an item as critical or medium severity. |

## MCP server

Pantau includes a Model Context Protocol (MCP) server, so AI agents can query the live
monitor. `docker compose up` starts the MCP server next to the site. The server serves
MCP over streamable HTTP at `/mcp`. In the local compose setup the endpoint is
`http://localhost:8001/mcp`. Behind a reverse proxy, route `/mcp` to the `pantau-mcp`
service. Example Caddy configuration:

```caddy
your.domain {
    handle /mcp* { reverse_proxy pantau-mcp:8000 }
    handle      { reverse_proxy pantau:8000 }
}
```

The server provides six tools: `situation_brief`, `live_streams`, `news_timeline`,
`reachability`, `markets`, and `city_activity`. The `news_timeline` tool can filter by
city or by critical severity. The server is a wrapper over `/api/summary` with a
15-second cache, so it exposes only the data that the public site already exposes.

## Chat

The dashboard can render a Chat tab. Set `CHAT_ENABLED=1` and route `/chat/*` on the
same origin to a backend with two endpoints: `GET /chat/stream` (server-sent events:
one `history` event with a JSON array, then one `msg` event per message) and
`POST /chat/post` (JSON body `{"user": "...", "text": "..."}`). The reference
deployment routes these to an SSH chat hub that also bridges a Discord channel, so
web, SSH, and Discord share one room.

## Architecture

One FastAPI service runs all collectors. Each collector is an async task that polls its
source on its own timer and writes normalized rows to SQLite at `data/pantau.db`. The
frontend is one static page that polls `/api/summary` every 30 seconds. The project has
no build step and no frontend framework. The application consists of `main.py` and one
`index.html`.

```
collectors ──> SQLite ──> /api/summary ──> index.html
```

## Deployment behind a domain

The container serves plain HTTP on `:8000`. Put your own reverse proxy in front of it.
`compose.yaml` contains commented Traefik labels. To use them, set `DOMAIN` in `.env`
and uncomment the labels. Nginx, Caddy, and Cloudflare Tunnel also work.

## License

Pantau is licensed under the MIT license. See [LICENSE](LICENSE).
