#!/usr/bin/env python3
"""Fetch Tufts SIS class-search data into static JSON for the web app.

Establishes a guest session against the SIS portal, pulls the full class
search dump for each active term, then fetches per-section enrollment
details. Writes data/<term>.json and data/index.json.

Stdlib only — safe to run on a bare GitHub Actions runner.
"""

import concurrent.futures
import datetime
import gzip
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORTAL_URL = "https://sis.it.tufts.edu/psp/paprd/EMPLOYEE/EMPL/h/?tab=TFP_CLASS_SEARCH"
BASE = "https://siscs.it.tufts.edu/psc/csprd/EMPLOYEE/HRMS/s/WEBLIB_CLS_SRCH.ISCRIPT1.FieldFormula."
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
USER_AGENT = "class-numbers/1.0 (static class search mirror; github.com/shaananc/class-numbers)"
DETAIL_WORKERS = 10

cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
opener.addheaders = [("User-Agent", USER_AGENT), ("Accept-Encoding", "gzip")]


def get(url, timeout=60):
    with opener.open(url, timeout=timeout) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body.decode("utf-8", "replace")


def jsonp(iscript, params, retries=3):
    qs = urllib.parse.urlencode({**params, "callback": "cb"})
    url = f"{BASE}{iscript}?{qs}"
    last = None
    for attempt in range(retries):
        try:
            text = get(url)
            m = re.search(r"^\s*cb\((.*)\)\s*;?\s*$", text, re.S)
            if m:
                return json.loads(m.group(1))
            last = RuntimeError(f"non-JSONP response ({text[:80]!r})")
        except Exception as e:  # noqa: BLE001 - retry any transport error
            last = e
        time.sleep(1.5 * (attempt + 1))
        establish_session()
    raise RuntimeError(f"{iscript} failed: {last}")


def establish_session():
    get(PORTAL_URL)


def term_label(code):
    year = 2000 + int(code[1:3])
    season = {"2": "Spring", "5": "Summer", "8": "Fall"}.get(code[3], "?")
    return f"{season} {year}"


def candidate_terms():
    """Term codes 2<yy><2|5|8> for terms that haven't ended yet (this year + next)."""
    now = datetime.date.today()
    end_month = {"2": 5, "5": 8, "8": 12}  # Spring ends May, Summer Aug, Fall Dec
    cands = []
    for year in (now.year, now.year + 1):
        for season in ("2", "5", "8"):
            if datetime.date(year, end_month[season], 28) >= now:
                cands.append(f"2{year % 100:02d}{season}")
    return cands


def probe_term(code):
    try:
        res = jsonp("IScript_getSearchresultsAll3", {
            "term": code, "career": "ALL", "subject": "CS",
            "course": "", "attr": "", "keyword": "", "instructor": "",
        }, retries=1)
        return bool(res.get("searchResults"))
    except Exception:
        return False


def compact_meeting(m):
    return {
        "days": m.get("days", []),
        "start": m.get("meet_start", ""),
        "end": m.get("meet_end", ""),
        "start_min": m.get("meet_start_min"),
    }


def fetch_details(term, class_num):
    d = jsonp("IScript_getResultsDetails", {"term": term, "class_num": class_num})
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
    if "wait" not in out:
        out["wait"] = int(d.get("wait_tot") or 0)
        out["wait_cap"] = int(d.get("wait_cap") or 0)
    for src, dst in (("note", "note"), ("req", "req"), ("Instruction_mode", "mode"),
                     ("start_date", "start_date"), ("end_date", "end_date"),
                     ("career", "career_desc"), ("addcons_desc", "consent"),
                     ("gradebase_desc", "grading")):
        val = d.get(src)
        if val:
            out[dst] = val
    return out


def build_term(term):
    print(f"[{term}] fetching search dump…", flush=True)
    dump = jsonp("IScript_getSearchresultsAll3", {
        "term": term, "career": "ALL", "subject": "",
        "course": "", "attr": "", "keyword": "", "instructor": "",
    })
    results = dump.get("searchResults", [])
    courses = []
    class_nums = []
    for c in results:
        if c.get("showCourse") == "N":
            continue
        course = {
            "num": c.get("course_num", ""),
            "title": c.get("course_title", ""),
            "career": c.get("acad_career", ""),
            "desc": c.get("desc_long", ""),
            "sections": [],
        }
        for sec in c.get("sections", []):
            for comp in sec.get("components", []):
                if comp.get("showClass") == "N":
                    continue
                instructors, meetings, rooms = [], [], []
                for loc in comp.get("locations", []):
                    name = (loc.get("instructor") or "").strip()
                    if name and name not in instructors:
                        instructors.append(name)
                    room = (loc.get("class_loc") or "").strip()
                    if room and room not in rooms:
                        rooms.append(room)
                    for m in loc.get("meetings", []):
                        cm = compact_meeting(m)
                        if cm not in meetings:
                            meetings.append(cm)
                section = {
                    "sec": comp.get("section_num", ""),
                    "class_num": comp.get("class_num", ""),
                    "component": comp.get("component", ""),
                    "status": comp.get("status", ""),
                    "campus": comp.get("campus", ""),
                    "session": comp.get("session_desc", ""),
                    "units_min": comp.get("unit_min"),
                    "units_max": comp.get("unit_max"),
                    "attrs": comp.get("class_attr", ""),
                    "instructors": instructors,
                    "rooms": rooms,
                    "meetings": meetings,
                }
                course["sections"].append(section)
                if section["class_num"]:
                    class_nums.append(section["class_num"])
        if course["sections"]:
            courses.append(course)

    print(f"[{term}] {len(courses)} courses, {len(class_nums)} sections; "
          f"fetching enrollment details…", flush=True)
    details = {}
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = {pool.submit(fetch_details, term, n): n for n in class_nums}
        for i, fut in enumerate(concurrent.futures.as_completed(futures)):
            num = futures[fut]
            try:
                details[num] = fut.result()
            except Exception as e:  # noqa: BLE001 - one bad section shouldn't kill the run
                failed += 1
                if failed <= 5:
                    print(f"[{term}] detail fetch failed for {num}: {e}", flush=True)
            if (i + 1) % 500 == 0:
                print(f"[{term}] details {i + 1}/{len(class_nums)}", flush=True)
    if failed:
        print(f"[{term}] {failed} detail fetches failed", flush=True)
    if failed > len(class_nums) // 4:
        raise RuntimeError(f"[{term}] too many detail failures ({failed}); aborting term")

    for course in courses:
        for section in course["sections"]:
            section.update(details.get(section["class_num"], {}))

    return {
        "term": term,
        "label": term_label(term),
        "generated": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "courses": courses,
    }


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    establish_session()

    terms = [t for t in candidate_terms() if probe_term(t)]
    if not terms:
        print("no active terms found — leaving existing data untouched", file=sys.stderr)
        sys.exit(1)
    print(f"active terms: {', '.join(terms)}", flush=True)

    index = []
    for term in terms:
        try:
            data = build_term(term)
        except Exception as e:  # noqa: BLE001 - keep previous data for a term that fails
            print(f"[{term}] FAILED: {e}", file=sys.stderr)
            old = os.path.join(DATA_DIR, f"{term}.json")
            if os.path.exists(old):
                with open(old) as f:
                    prev = json.load(f)
                index.append({"term": term, "label": prev["label"],
                              "generated": prev["generated"],
                              "courses": len(prev["courses"])})
            continue
        with open(os.path.join(DATA_DIR, f"{term}.json"), "w") as f:
            json.dump(data, f, separators=(",", ":"))
        index.append({"term": term, "label": data["label"],
                      "generated": data["generated"],
                      "courses": len(data["courses"])})

    if not index:
        print("every term failed — aborting without writing index", file=sys.stderr)
        sys.exit(1)
    with open(os.path.join(DATA_DIR, "index.json"), "w") as f:
        json.dump(index, f, separators=(",", ":"))
    print("done", flush=True)


if __name__ == "__main__":
    main()
