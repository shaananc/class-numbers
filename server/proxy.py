#!/usr/bin/env python3
"""Live enrollment proxy for the Tufts Class Explorer.

Sits on starkiller behind Tailscale Funnel and answers
GET /details?term=2268&class_nums=83571,83572 with fresh enrollment
numbers from SIS, doing the guest-session dance server-side.

Caching design:
- one shared guest session, re-established on login redirect (rate-limited)
- per-(term,class_num) cache: 60s fresh TTL, stale-while-revalidate up to
  24h (stale entries are served instantly and refreshed in background)
- single-flight per key so concurrent misses make one upstream request
- 20s negative cache on upstream failure
- Cache-Control: max-age=30 so a single browser dedupes its own re-clicks

Stdlib only.
"""

import http.cookiejar
import http.server
import json
import re
import threading
import time
import urllib.parse
import urllib.request

LISTEN = ("127.0.0.1", 8300)
PORTAL_URL = "https://sis.it.tufts.edu/psp/paprd/EMPLOYEE/EMPL/h/?tab=TFP_CLASS_SEARCH"
DETAILS_URL = ("https://siscs.it.tufts.edu/psc/csprd/EMPLOYEE/HRMS/s/"
               "WEBLIB_CLS_SRCH.ISCRIPT1.FieldFormula.IScript_getResultsDetails")
USER_AGENT = "class-numbers-live/1.0 (github.com/shaananc/class-numbers)"

FRESH_TTL = 60          # serve without upstream contact
STALE_TTL = 24 * 3600   # serve immediately but refresh in background
NEG_TTL = 20            # remember failures briefly
MAX_BATCH = 60
UPSTREAM_CONCURRENCY = 8

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_opener.addheaders = [("User-Agent", USER_AGENT)]
_session_lock = threading.Lock()
_last_auth = 0.0

_cache = {}             # key -> (fetched_at, data | None)
_cache_lock = threading.Lock()
_inflight = {}          # key -> threading.Event
_upstream_sem = threading.Semaphore(UPSTREAM_CONCURRENCY)


def _establish_session(force=False):
    global _last_auth
    with _session_lock:
        if not force and time.time() - _last_auth < 30:
            return  # someone else just re-authed
        _opener.open(PORTAL_URL, timeout=30).read()
        _last_auth = time.time()


def _upstream_details(term, class_num):
    qs = urllib.parse.urlencode({"term": term, "class_num": class_num, "callback": "cb"})
    for attempt in (1, 2):
        with _upstream_sem:
            body = _opener.open(f"{DETAILS_URL}?{qs}", timeout=30).read().decode("utf-8", "replace")
        m = re.search(r"^\s*cb\((.*)\)\s*;?\s*$", body, re.S)
        if m:
            return json.loads(m.group(1))
        if attempt == 1:  # login redirect page — session expired
            _establish_session(force=True)
    raise RuntimeError("upstream returned non-JSONP twice")


def _compact(d):
    out = {}
    for row in d.get("reserved_cap", []):
        if row.get("cap_type") == "Enrollment":
            out["enrolled"] = int(row.get("total") or 0)
            out["cap"] = int(row.get("cap") or 0)
        elif row.get("cap_type") == "Wait List":
            out["wait"] = int(row.get("total") or 0)
            out["wait_cap"] = int(row.get("cap") or 0)
    if "enrolled" not in out:
        out["enrolled"] = int(d.get("enroll_tot") or 0)
        out["cap"] = int(d.get("enroll_cap") or 0)
    return out


def _refresh(key):
    """Fetch one (term, class_num) into the cache; single-flight."""
    with _cache_lock:
        if key in _inflight:
            ev = _inflight[key]
            waiter = True
        else:
            ev = _inflight[key] = threading.Event()
            waiter = False
    if waiter:
        ev.wait(timeout=35)
        with _cache_lock:
            return _cache.get(key)
    try:
        data = _compact(_upstream_details(*key))
        entry = (time.time(), data)
    except Exception:
        entry = (time.time(), None)  # negative entry
    with _cache_lock:
        prev = _cache.get(key)
        # don't clobber good data with a failure; just bump its refresh time
        if entry[1] is None and prev and prev[1] is not None:
            entry = (prev[0] + NEG_TTL, prev[1])
        _cache[key] = entry
        _inflight.pop(key, None).set()
        return entry


def get_details(term, class_num):
    """Returns (data|None, is_stale). Serves stale instantly, refreshes in background."""
    key = (term, class_num)
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
    if entry:
        age = now - entry[0]
        if entry[1] is not None and age < FRESH_TTL:
            return entry[1], False
        if entry[1] is None and age < NEG_TTL:
            return None, False
        if entry[1] is not None and age < STALE_TTL:
            threading.Thread(target=_refresh, args=(key,), daemon=True).start()
            return entry[1], True
    entry = _refresh(key)
    if entry and entry[1] is not None:
        return entry[1], False
    return None, False


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj, max_age=30):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", f"public, max-age={max_age}")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/healthz":
            with _cache_lock:
                n = len(_cache)
            return self._send(200, {"ok": True, "cached": n}, max_age=0)
        if url.path != "/details":
            return self._send(404, {"error": "not found"}, max_age=0)
        q = urllib.parse.parse_qs(url.query)
        term = (q.get("term") or [""])[0]
        nums = re.split(r"[,\s]+", (q.get("class_nums") or [""])[0].strip())
        nums = [n for n in nums if n]
        if not re.fullmatch(r"2\d{3}", term) or not nums or len(nums) > MAX_BATCH \
                or not all(re.fullmatch(r"\d{1,6}", n) for n in nums):
            return self._send(400, {"error": "bad params"}, max_age=0)

        results, any_stale = {}, False
        threads, out = [], {}

        def work(n):
            out[n] = get_details(term, n)

        for n in nums:
            t = threading.Thread(target=work, args=(n,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=40)
        for n, (data, stale) in out.items():
            if data is not None:
                results[n] = data
                any_stale = any_stale or stale
        self._send(200, {"results": results, "stale": any_stale,
                         "ts": int(time.time())})

    def log_message(self, fmt, *args):
        pass  # keep journal quiet; systemd captures errors via tracebacks


def main():
    _establish_session(force=True)
    srv = http.server.ThreadingHTTPServer(LISTEN, Handler)
    print(f"listening on {LISTEN[0]}:{LISTEN[1]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
