# Tufts Class Explorer

Fast, no-login search of Tufts classes with enrollment numbers — a friendlier
front-end to the data behind the SIS public class search.

**Live site:** https://shaananc.github.io/class-numbers/

## How it works

- [`scripts/fetch_data.py`](scripts/fetch_data.py) establishes a guest session
  against the SIS portal, pulls the full class-search dump for each active term
  (`IScript_getSearchresultsAll3`), then fetches per-section enrollment details
  (`IScript_getResultsDetails`) and writes compact JSON to `data/`.
- A [GitHub Actions workflow](.github/workflows/refresh.yml) reruns the fetch
  hourly during the day (every 3 h overnight), commits the data, and redeploys
  GitHub Pages. The page itself polls for fresh data every 5 minutes, so an
  open tab stays current without a reload.
- [`index.html`](index.html) is a single-file static app: it loads the JSON
  once and searches entirely client-side, so results are instant.

The SIS endpoints require session cookies that browsers won't send cross-site,
so a pure client-side app can't call them directly — that's why the data is
mirrored by the scheduled workflow instead.

## Live enrollment layer

[`server/proxy.py`](server/proxy.py) runs in a dedicated LXC container
(`class-live`, CT 130) on a Proxmox host, as the systemd unit in
[`server/class-live.service`](server/class-live.service). The hypervisor's
tailscaled fronts it over HTTPS (`tailscale serve`/Funnel proxying to the
container) at `starkiller.tail283e6.ts.net`. When a class card is
expanded, the page fetches live numbers from it (and re-polls open cards every
60 s); if the proxy is unreachable the page silently falls back to the scraped
data. Caching: shared guest session, per-class 60 s TTL with
stale-while-revalidate (24 h), single-flight deduplication, 20 s negative
cache, and `Cache-Control: max-age=30` toward browsers — so SIS sees at most
one request per class per minute regardless of visitor count.

## JSON API

Everything below is public, CORS-open (`Access-Control-Allow-Origin: *`), and
needs no key.

**Bulk data** (served by GitHub Pages, updated hourly):
- `GET /data/index.json` — list of terms with course counts and last-scrape time
- `GET /data/<term>.json` — full catalog for a term (all courses + sections;
  the Fall file is ~4 MB, so prefer the endpoints below for single lookups)

**Live queries** (served by `starkiller.tail283e6.ts.net`, backed by the same
proxy described above):
- `GET /api/terms` — mirrors `data/index.json`
- `GET /api/search?term=2268&q=cs+security&limit=25` — full-text search over
  course number (with or without dashes/leading zeros), title, and instructor;
  returns matching courses with their last-scraped enrollment (not live)
- `GET /api/course/<term>/<num>` — one course by number (`CS-0151`, `cs151`,
  `CS 0151` all resolve), with **live** enrollment merged into each section
  (`"live": true`, `"stale": true` if served from the stale-while-revalidate
  cache while a refresh is in flight). 404 if the number doesn't exist that term.
- `GET /details?term=2268&class_nums=83570,83571` — the low-level batch
  endpoint the frontend uses; raw live enrollment by class number, no catalog
  lookup

Known limitation: if two courses share a compact alias (e.g. two different
special-topics sections both numbered `CS-0151`), `/api/course` returns
whichever was indexed last — use `/api/search` to see all of them.

## Local development

```sh
python3 scripts/fetch_data.py   # seeds data/ (takes a few minutes)
python3 -m http.server 8000     # then open http://localhost:8000
```

Unofficial; not affiliated with Tufts. Enrollment numbers are a few hours
stale — confirm in SIS before acting on them.
