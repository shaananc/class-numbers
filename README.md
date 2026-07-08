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

## Local development

```sh
python3 scripts/fetch_data.py   # seeds data/ (takes a few minutes)
python3 -m http.server 8000     # then open http://localhost:8000
```

Unofficial; not affiliated with Tufts. Enrollment numbers are a few hours
stale — confirm in SIS before acting on them.
