#!/usr/bin/env python3
"""
Regenerate the built-in NOAA tide table embedded in index.html.

WHY THIS EXISTS
---------------
The board carries a year of official NOAA high/low predictions inside the page
so it works with no signal, which is the whole point of a boat tool. v1 of the
board embedded that table by hand and, when it ran out, printed an error telling
the user to "ask Claude for a refreshed version" — a dead end for anyone who is
not Jan, and a dead end for Jan in a year's time.

This script is that refresh. It is the same shape as the WTF regs pipeline: a
scheduled job regenerates a data blob, commits it, and nobody has to remember.

WHAT IT WRITES
--------------
Three consecutive lines in index.html, between the /*__FALLBACK__*/ marker (on
a first run) or in place of the previous block:

    const FALLBACK_BASE = new Date(YYYY, M-1, D, 0, 0);
    const FALLBACK_END  = new Date(YYYY, M-1, D, 0, 0);
    const FALLBACK_DATA = "<minutes>,<height*100>,<1=H|0=L>;...";

Minutes are counted from FALLBACK_BASE in LOCAL time, matching how the page
reconstructs them. Heights are hundredths of a foot above MLLW.

USAGE
-----
    python3 tools/refresh_fallback.py                 # 14 months from today
    python3 tools/refresh_fallback.py --months 18
    python3 tools/refresh_fallback.py --check         # exit 3 if a refresh is due

Only stdlib — it runs on a bare GitHub Actions runner with no pip install.
"""

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

STATION = "8721138"                     # Halifax River, Ponce Inlet
API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"

# Refresh when the embedded table has less than this much life left in it. A
# year of runway means a missed run is a non-event rather than an outage.
MIN_REMAINING_DAYS = 300


def fetch_range(begin: dt.date, end: dt.date):
    """NOAA caps a hilo request at about a year, so this is called in chunks."""
    url = (
        f"{API}?product=predictions&application=halifax_tide_board"
        f"&begin_date={begin:%Y%m%d}&end_date={end:%Y%m%d}"
        f"&datum=MLLW&station={STATION}"
        f"&time_zone=lst_ldt&units=english&interval=hilo&format=json"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "tide-board-refresh"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    if "predictions" not in data:
        raise RuntimeError(data.get("error", {}).get("message", "no predictions returned"))
    return data["predictions"]


def collect(base: dt.date, months: int):
    """Walk the span in ~11-month chunks and return sorted, de-duplicated events."""
    end = base + dt.timedelta(days=int(months * 30.44))
    seen, rows = set(), []
    cur = base
    while cur < end:
        stop = min(cur + dt.timedelta(days=330), end)
        for p in fetch_range(cur, stop):
            t = dt.datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
            if t in seen:
                continue
            seen.add(t)
            rows.append((t, float(p["v"]), p["type"]))
        cur = stop + dt.timedelta(days=1)
    rows.sort(key=lambda r: r[0])
    return rows, end


def encode(rows, base_dt):
    out = []
    for t, v, typ in rows:
        mins = int((t - base_dt).total_seconds() // 60)
        if mins < 0:
            continue
        out.append(f"{mins},{round(v * 100)},{1 if typ == 'H' else 0}")
    return ";".join(out)


BLOCK_RE = re.compile(
    r"(?:/\*__FALLBACK__\*/)|"
    r"(?:const FALLBACK_BASE = new Date\([^;]*\);\s*"
    r"const FALLBACK_END  = new Date\([^;]*\);\s*"
    r"const FALLBACK_DATA = \"[^\"]*\";)",
    re.S,
)


def current_end(text):
    m = re.search(r"const FALLBACK_END  = new Date\((\d+), (\d+), (\d+),", text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return dt.date(y, mo + 1, d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=14)
    ap.add_argument("--check", action="store_true",
                    help="exit 3 if the embedded table is running out, 0 if it is fine")
    args = ap.parse_args()

    text = INDEX.read_text()

    if args.check:
        end = current_end(text)
        if end is None:
            print("no embedded table found — refresh needed")
            return 3
        left = (end - dt.date.today()).days
        print(f"embedded table runs to {end} ({left} days left)")
        return 3 if left < MIN_REMAINING_DAYS else 0

    base = dt.date.today()
    base_dt = dt.datetime(base.year, base.month, base.day)
    rows, end = collect(base, args.months)
    if len(rows) < 500:
        raise SystemExit(f"only {len(rows)} predictions came back — refusing to write a short table")

    block = (
        f"const FALLBACK_BASE = new Date({base.year}, {base.month - 1}, {base.day}, 0, 0);\n"
        f"const FALLBACK_END  = new Date({end.year}, {end.month - 1}, {end.day}, 0, 0);\n"
        f'const FALLBACK_DATA = "{encode(rows, base_dt)}";'
    )

    if not BLOCK_RE.search(text):
        raise SystemExit("could not find the fallback block or its marker in index.html")
    INDEX.write_text(BLOCK_RE.sub(lambda _: block, text, count=1))
    print(f"wrote {len(rows)} predictions, {base} to {end} "
          f"({len(block) / 1024:.0f} KB) into {INDEX.name}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as e:
        raise SystemExit(f"could not reach NOAA: {e}")
