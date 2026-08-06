#!/usr/bin/env python3
"""
Read the board's surveyed depth at a point, outside the browser.

A faithful port of depthAtPoint() in index.html: same blocks, same 1.3 km
search radius, same interpolate-between-the-two-nearest-contour-levels, same
NAVD88 -> MLLW offset. It exists so a number on the board can be checked
against a chart, a plotter or a sounding without opening the app and tapping.

    python3 tools/depth_at.py 29.0779 -80.9085
    python3 tools/depth_at.py --grid 29.065 29.085 -80.945 -80.905 --step 0.002
    python3 tools/depth_at.py --spots            # every built-in card

Output is depth at MLLW — chart-sounding datum, directly comparable to what a
chart or a plotter prints. The blocks themselves are NAVD88, which stands
2.25 ft ABOVE MLLW at Ponce, so that offset is subtracted here exactly as the
app subtracts it.

The number is only ever as good as the contour interval, and much worse than
that when the nearest contour is far away — `near` in the output is the
distance to the closest contour of the winning level, in metres. Past ~200 m
the app calls it rough; past 600 m it refuses to print a depth at all. Treat
those the same way here.
"""

import sys
import os
import json
import math

DEPTH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'depth')
INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'index.html')

NAVD88_ABOVE_MLLW = 2.25
SEARCH_DEG = 0.012          # ~1.3 km, same as the app
NEAR_M, FAR_M = 200, 600    # the app's confidence bands

BLOCKS = [
    ('c3_4', -81.2, 29.0, -81.0, 29.25), ('c3_5', -81.2, 29.25, -81.0, 29.48),
    ('c4_2', -81.0, 28.5, -80.75, 28.75), ('c4_3', -81.0, 28.75, -80.75, 29.0),
    ('c4_4', -81.0, 29.0, -80.75, 29.25), ('c4_5', -81.0, 29.25, -80.75, 29.48),
]

_cache = {}


def block_for(lat, lng):
    for k, w, s, e, n in BLOCKS:
        if w <= lng <= e and s <= lat <= n:
            return k
    return None


def load_block(key):
    """Decode one block: [level, tier, x0, y0, dx, dy, ...] deltas at 1e-5 deg."""
    if key in _cache:
        return _cache[key]
    path = os.path.join(DEPTH_DIR, key + '.json')
    if not os.path.exists(path):
        _cache[key] = None
        return None
    raw = json.load(open(path))
    w, s, q = raw['b'][0], raw['b'][1], raw['q']
    lines = []
    for L in raw['L']:
        n = (len(L) - 2) >> 1
        if n < 2:
            continue
        x, y = L[2], L[3]
        pts = [(w + x / q, s + y / q)]
        for i in range(1, n):
            x += L[2 + i * 2]
            y += L[3 + i * 2]
            pts.append((w + x / q, s + y / q))
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        lines.append({'lv': L[0], 'pts': pts,
                      'bb': (min(xs), min(ys), max(xs), max(ys))})
    _cache[key] = {'lines': lines}
    return _cache[key]


def seg_dist(px, py, ax, ay, bx, by, kx):
    dx, dy = (bx - ax) * kx, by - ay
    wx, wy = (px - ax) * kx, py - ay
    l2 = dx * dx + dy * dy
    t = (wx * dx + wy * dy) / l2 if l2 > 0 else 0.0
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    ex, ey = wx - t * dx, wy - t * dy
    return math.hypot(ex, ey)


def depth_at(lat, lng):
    """-> dict(navd88, mllw, lo, hi, near_m, single) or None."""
    key = block_for(lat, lng)
    if not key:
        return None
    blk = load_block(key)
    if not blk:
        return None
    kx = math.cos(math.radians(lat))
    best = {}
    for ln in blk['lines']:
        bb = ln['bb']
        if (lng < bb[0] - SEARCH_DEG or lng > bb[2] + SEARCH_DEG
                or lat < bb[1] - SEARCH_DEG or lat > bb[3] + SEARCH_DEG):
            continue
        d = min(seg_dist(lng, lat, a[0], a[1], b[0], b[1], kx)
                for a, b in zip(ln['pts'], ln['pts'][1:]))
        if d > SEARCH_DEG:
            continue
        if ln['lv'] not in best or d < best[ln['lv']]:
            best[ln['lv']] = d
    if not best:
        return None
    ordered = sorted(best.items(), key=lambda kv: kv[1])
    (lv1, d1) = ordered[0]
    deg_m = 111320.0
    if len(ordered) == 1:
        return {'navd88': float(lv1), 'mllw': lv1 - NAVD88_ABOVE_MLLW,
                'lo': lv1, 'hi': lv1, 'near_m': d1 * deg_m, 'single': True}
    (lv2, d2) = ordered[1]
    tot = d1 + d2
    ft = lv1 + (lv2 - lv1) * (d1 / tot) if tot > 0 else float(lv1)
    return {'navd88': ft, 'mllw': ft - NAVD88_ABOVE_MLLW,
            'lo': min(lv1, lv2), 'hi': max(lv1, lv2),
            'near_m': d1 * deg_m, 'single': False}


def flag(near_m):
    if near_m > FAR_M:
        return 'NO CALL (contour %.0f m away)' % near_m
    if near_m > NEAR_M:
        return 'rough (%.0f m)' % near_m
    return 'ok (%.0f m)' % near_m


def show(lat, lng, label=''):
    d = depth_at(lat, lng)
    if not d:
        print('%-26s %9.5f %10.5f   no contour within 1.3 km' % (label, lat, lng))
        return
    band = ('%d ft line' % d['lo']) if d['single'] else ('%d-%d' % (d['lo'], d['hi']))
    print('%-26s %9.5f %10.5f  %6.1f ft MLLW  (NAVD88 %5.1f, between %s)  %s'
          % (label, lat, lng, d['mllw'], d['navd88'], band, flag(d['near_m'])))


def spots():
    import re
    src = open(INDEX_HTML, encoding='utf-8').read()
    pat = re.compile(r'\{\s*key:"[^"]+",\s*name:"((?:[^"\\]|\\.)*)".*?'
                     r'lat:(-?\d+\.?\d*),\s*lng:(-?\d+\.?\d*)', re.S)
    return [(m.group(1).replace('\\"', '"').replace("\\'", "'"),
             float(m.group(2)), float(m.group(3))) for m in pat.finditer(src)]


def main(argv):
    if '--spots' in argv:
        for name, lat, lng in spots():
            show(lat, lng, name)
        return 0
    if '--grid' in argv:
        i = argv.index('--grid')
        lat0, lat1, lng0, lng1 = (float(x) for x in argv[i + 1:i + 5])
        step = 0.002
        if '--step' in argv:
            step = float(argv[argv.index('--step') + 1])
        lat = lat0
        while lat <= lat1 + 1e-9:
            lng = lng0
            while lng <= lng1 + 1e-9:
                show(lat, lng)
                lng += step
            lat += step
        return 0
    if len(argv) >= 2:
        show(float(argv[0]), float(argv[1]))
        return 0
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
