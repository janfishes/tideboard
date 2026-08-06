#!/usr/bin/env python3
"""
Stage 1 of getting Jan's Garmin ECHOMAP CHIRP 74sv soundings into this board.

READ ONLY. It converts nothing and writes nothing. Its whole job is to answer
the one question the Garmin documentation will not: does a track exported from
THIS unit's firmware carry a depth on every trackpoint, or only a position?

Garmin and Humminbird both extend the GPX track format with a per-point depth
(plain GPX has no such field, which is why this normally goes nowhere). Newer
ECHOMAPs write it. The 74sv is 2016-era firmware and Garmin's own manual
describes GPX export as being for "waypoints and routes" without mentioning
tracks at all, so it has to be measured on a real file rather than assumed.

Three outcomes, and the answer decides the whole pipeline:

  depth on trackpoints  -> a saved track IS a survey. Stage 2 reduces every
                           point to MLLW against the tide at its own timestamp.
  depth on waypoints    -> fall back to marking a waypoint over each spot. The
                           74sv stores depth in a waypoint record. Slow, but
                           it works.
  no depth anywhere     -> the sonar log (.RSD) is the only path left, and that
                           needs ReefMaster. Probably not worth it.

Usage:
    python3 tools/inspect_garmin_gpx.py /Volumes/<card>/Garmin/UserData/*.gpx

Getting the file off the plotter (the order matters):
  1. Nav Info > Tracks > Save Active Track     <- the live track is NOT included
                                                  in a user-data export until it
                                                  is saved
  2. Nav Info > User Data > Data Transfer > File Type > GPX
                                                <- default is .adm, which only
                                                   another Garmin can read
  3. User Data > Manage Data > Data Transfer > Save to Card
  4. The card is the ActiveCaptain card (the 74sv has one slot). Do not let
     anything reformat it. Files land in /Garmin/UserData/.

ActiveCaptain cannot do this. It syncs waypoints and routes only — tracks are
not in user-data sync and the app has no GPX export.

Depths reported here are RAW, exactly as the sounder wrote them. Two corrections
are still owed before any of it means anything, and both belong to stage 2:
  - the transducer offset (waterline vs transducer face), and
  - the tide standing at that moment, from that spot's own lag and height model.
Only after both is a number comparable to a chart sounding or to soundedFt.
"""

import sys
import re
import glob
import math
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

INDEX_HTML = '/Users/janneal/Documents/tides/index.html'
NEAR_YDS = 75          # how close a point has to be to count as "at" a spot
M_PER_YD = 0.9144


# ---------------------------------------------------------------- spots

def load_spots(path=INDEX_HTML):
    """Pull key/name/lat/lng out of the LOCATIONS array in index.html.

    Regex rather than a parser because the array is hand-written JS wrapped in
    long comments, and the four fields wanted are always on the same two lines
    in the same order. A miss here costs a per-spot report, not the run — the
    depth answer never depends on it.

    Built-in cards only (7 of them). Spots added inside the app live in
    localStorage on the device, so a point sitting over one of those is
    reported as near nothing.
    """
    try:
        src = open(path, encoding='utf-8').read()
    except OSError:
        return []
    spots = []
    pat = re.compile(
        r'\{\s*key:"(?P<key>[^"]+)",\s*name:"(?P<name>(?:[^"\\]|\\.)*)"'
        r'.*?lat:(?P<lat>-?\d+\.?\d*),\s*lng:(?P<lng>-?\d+\.?\d*)',
        re.S)
    for m in pat.finditer(src):
        spots.append({
            'key': m.group('key'),
            'name': m.group('name').replace('\\"', '"').replace("\\'", "'"),
            'lat': float(m.group('lat')),
            'lng': float(m.group('lng')),
        })
    return spots


def haversine_m(a_lat, a_lng, b_lat, b_lng):
    r = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- gpx

def localname(tag):
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def namespaces_in(root):
    ns = set()
    for el in root.iter():
        if el.tag.startswith('{'):
            ns.add(el.tag[1:].split('}', 1)[0])
    return sorted(ns)


def find_depth(el):
    """Depth for one wpt/trkpt, in metres, or None.

    Garmin and Humminbird each nest it differently and the namespace has moved
    across firmware generations, so this matches on the LOCAL name anywhere
    underneath the point rather than on a fixed path. <depth> is also legal
    plain-GPX on a waypoint. Anything whose local name is exactly "depth" wins;
    otherwise the first tag containing "depth" is taken (seen: DepthValue,
    gpxx:Depth).
    """
    exact, loose = None, None
    for sub in el.iter():
        if sub is el:
            continue
        name = localname(sub.tag).lower()
        if 'depth' not in name or not (sub.text or '').strip():
            continue
        try:
            v = float(sub.text.strip())
        except ValueError:
            continue
        if name == 'depth' and exact is None:
            exact = v
        elif loose is None:
            loose = v
    return exact if exact is not None else loose


def parse_time(el):
    t = None
    for sub in el:
        if localname(sub.tag) == 'time' and (sub.text or '').strip():
            t = sub.text.strip()
            break
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace('Z', '+00:00'))
    except ValueError:
        return None


def read_points(root, kind):
    """kind: 'wpt' for waypoints, 'trkpt' for every point of every track."""
    pts = []
    for el in root.iter():
        if localname(el.tag) != kind:
            continue
        try:
            lat = float(el.attrib['lat'])
            lng = float(el.attrib['lon'])
        except (KeyError, ValueError):
            continue
        pts.append({
            'lat': lat, 'lng': lng,
            'depth_m': find_depth(el),
            'time': parse_time(el),
            'name': next((s.text for s in el
                          if localname(s.tag) == 'name' and s.text), None),
        })
    return pts


# ---------------------------------------------------------------- report

def fmt_span(times):
    times = sorted(t for t in times if t)
    if not times:
        return 'no timestamps'
    a, b = times[0], times[-1]
    mins = (b - a).total_seconds() / 60
    loc = lambda t: t.astimezone().strftime('%Y-%m-%d %H:%M')
    return '%s -> %s local (%.0f min)' % (loc(a), loc(b), mins)


def spacing_report(pts):
    """Metres and seconds between consecutive trackpoints."""
    d, s = [], []
    for a, b in zip(pts, pts[1:]):
        d.append(haversine_m(a['lat'], a['lng'], b['lat'], b['lng']))
        if a['time'] and b['time']:
            gap = (b['time'] - a['time']).total_seconds()
            if 0 < gap < 3600:
                s.append(gap)
    out = []
    if d:
        out.append('spacing median %.0f ft, max %.0f ft'
                   % (statistics.median(d) / M_PER_YD * 3,
                      max(d) / M_PER_YD * 3))
    if s:
        out.append('interval median %.0f s' % statistics.median(s))
    return '; '.join(out) if out else 'not enough points to measure'


def near_spots(pts, spots):
    rows = []
    limit = NEAR_YDS * M_PER_YD
    for sp in spots:
        hits = [p for p in pts
                if haversine_m(sp['lat'], sp['lng'], p['lat'], p['lng']) <= limit]
        if not hits:
            continue
        withd = [p['depth_m'] for p in hits if p['depth_m'] is not None]
        rows.append((sp['name'], len(hits), withd))
    rows.sort(key=lambda r: -r[1])
    return rows


def inspect(path, spots):
    print('=' * 74)
    print(path)
    print('=' * 74)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        print('  NOT PARSEABLE as XML: %s' % e)
        print('  If this came off the plotter as .adm, the File Type was not set')
        print('  to GPX before saving. Re-export, or convert with adm2gpx.')
        return

    ns = namespaces_in(root)
    print('\nnamespaces:')
    for n in ns:
        print('  %s' % n)
    vendor = [n for n in ns if 'garmin' in n.lower() or 'humminbird' in n.lower()]
    print('  -> vendor extension %s'
          % ('PRESENT: ' + ', '.join(vendor) if vendor else 'ABSENT'))

    wpts = read_points(root, 'wpt')
    trkpts = read_points(root, 'trkpt')
    ntrk = sum(1 for el in root.iter() if localname(el.tag) == 'trk')

    wd = [p for p in wpts if p['depth_m'] is not None]
    td = [p for p in trkpts if p['depth_m'] is not None]

    print('\nwaypoints:   %5d   (%d carry a depth)' % (len(wpts), len(wd)))
    print('tracks:      %5d   with %d points total (%d carry a depth)'
          % (ntrk, len(trkpts), len(td)))

    print('\nVERDICT')
    if td:
        pct = 100.0 * len(td) / len(trkpts)
        print('  DEPTH IS ON TRACKPOINTS (%.0f%% of them).' % pct)
        print('  Saved tracks from this unit are usable as a survey. Stage 2 is on.')
        if pct < 95:
            print('  Note the gap: points without depth are usually the sounder')
            print('  losing bottom — planing speed, aeration, or over 200 ft.')
    elif wd:
        print('  NO depth on trackpoints, but %d waypoints carry one.' % len(wd))
        print('  Fall back to marking a waypoint over each spot; the track is')
        print('  then only a position record.')
    else:
        print('  NO DEPTH ANYWHERE in this file.')
        print('  Before concluding the firmware cannot do it, check that the')
        print('  export happened with a transducer connected and that the active')
        print('  track was SAVED first (Nav Info > Tracks > Save Active Track).')

    for label, pts in (('waypoint', wd), ('trackpoint', td)):
        if not pts:
            continue
        dm = [p['depth_m'] for p in pts]
        print('\n%s depths, RAW (uncorrected for transducer offset or tide):' % label)
        print('  metres: min %.1f  median %.1f  max %.1f' % (min(dm), statistics.median(dm), max(dm)))
        print('  feet:   min %.1f  median %.1f  max %.1f'
              % (min(dm) * 3.28084, statistics.median(dm) * 3.28084, max(dm) * 3.28084))
        print('  (if these look like feet already, the unit exported feet —')
        print('   stage 2 must not convert twice)')

    if trkpts:
        print('\ntrack shape:')
        print('  %s' % fmt_span([p['time'] for p in trkpts]))
        print('  %s' % spacing_report(trkpts))

    if spots:
        for label, pts in (('waypoints', wpts), ('trackpoints', trkpts)):
            rows = near_spots(pts, spots)
            if not rows:
                continue
            print('\n%s within %d yds of a board spot:' % (label, NEAR_YDS))
            for name, n, withd in rows:
                extra = ''
                if withd:
                    extra = ('  depth %.1f-%.1f ft raw'
                             % (min(withd) * 3.28084, max(withd) * 3.28084))
                print('  %-26s %4d pts%s' % (name, n, extra))
    print()


def main(argv):
    paths = []
    for a in argv:
        paths.extend(sorted(glob.glob(a)) or [a])
    if not paths:
        print(__doc__)
        return 1
    spots = load_spots()
    print('board spots loaded: %d\n' % len(spots))
    for p in paths:
        inspect(p, spots)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
