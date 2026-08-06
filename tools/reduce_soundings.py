#!/usr/bin/env python3
"""
Stage 2: reduce a Garmin track to soundings at MLLW.

Stage 1 (inspect_garmin_gpx.py) answers "does this unit put depth on
trackpoints?". For the 74sv the answer is yes, so a saved track is a survey and
this is what turns it into numbers the board can use:

    depth at MLLW = sounder depth (to waterline) - tide height at that spot,
                    that minute

Every trackpoint carries its own timestamp, and that is the entire reason a
track beats a spot reading: one drift crosses tide states, and each point gets
its own correction rather than one reading you have to trust.

    python3 tools/reduce_soundings.py depth/tracks/8-6-2026.GPX
    python3 tools/reduce_soundings.py depth/tracks/*.GPX --csv out.csv
    python3 tools/reduce_soundings.py depth/tracks/8-6-2026.GPX --radius 150

UNITS.  The Garmin displays feet, but the GPX it writes is METRES -- the
gpxx/gpxtpx schemas define depth in metres and the unit converts on export.
Confirmed against known water on Jan's 8-6-2026 file: read as metres the ICW at
Dunlawton comes out 15.7-21.8 ft against a ~12 ft charted channel, and the
Ponce scour hole 69.8 ft; read as feet those become 4.8-6.6 ft and 21 ft, which
would have had him aground. So: metres in the file, converted ONCE here.

TRANSDUCER OFFSET.  Jan's unit carried a +1.0 ft offset on these tracks, which
on a Garmin is the transducer-to-waterline distance -- so exported depths are
ALREADY referenced to the water surface and nothing further is subtracted. A
negative offset would have meant depth-below-keel instead; --offset-mode says
which, if a future card was set up the other way. Keel draft (7-10 in) is not
part of this reduction at all: it belongs to clearance, not to datum.

THE TIDE MODEL is the board's own, ported from index.html so the two can never
disagree: per-spot lag (hi/lo minutes behind the inlet) and per-spot height
scaling (shown_high = hs*ref_high + ho), both interpolated by distance-from-
inlet the way estimateLag() does, then the cosine curve of tideAt() between the
bracketing NOAA events.

WHAT IS ESTIMATED, AND SAY SO.  The board deliberately refuses to guess height
scaling -- an uncalibrated spot keeps the inlet's heights rather than being
silently damped. That is right for a display, but useless here: using the
inlet's 2.7 ft swing at a spot that moves 1.5 ft would put ~1 ft of pure error
into every sounding upriver. So this DOES interpolate the scale, between the
measured anchors only, and marks every such point `est` in the output. Read a
column of est numbers as what it is: better than the inlet's swing, not a
calibration.
"""

import sys
import os
import re
import csv
import json
import math
import urllib.request
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, '..', 'index.html')
CACHE_DIR = os.path.join(HERE, '..', 'depth', 'tracks', '.preds')

LOCAL_TZ = ZoneInfo('America/New_York')   # NOAA lst_ldt for this station
M_TO_FT = 3.280839895
NM_PER_DEG = 60.0
STATION = '8721138'
DEFAULT_RADIUS_YD = 75

try:
    from depth_at import depth_at as survey_depth_at
except ImportError:
    sys.path.insert(0, HERE)
    try:
        from depth_at import depth_at as survey_depth_at
    except ImportError:
        survey_depth_at = None


# --------------------------------------------------------------------------
# the board's own tables, read out of index.html so there is one source
# --------------------------------------------------------------------------

def _strip_comments(s):
    return re.sub(r'/\*.*?\*/', '', s, flags=re.S)


def load_locations():
    """BUILTIN_LOCATIONS from index.html: lag (hi/lo) and height scale (hs/ho)."""
    src = open(INDEX_HTML, encoding='utf-8').read()
    i = src.index('const BUILTIN_LOCATIONS')
    i = src.index('[', i)
    depth, j = 0, i
    while j < len(src):
        if src[j] == '[':
            depth += 1
        elif src[j] == ']':
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = _strip_comments(src[i + 1:j])

    locs = []
    for m in re.finditer(r'\{[^{}]*\}', body):
        rec = m.group(0)
        def num(field):
            mm = re.search(r'\b' + field + r':\s*(-?\d+\.?\d*)', rec)
            return float(mm.group(1)) if mm else None
        key = re.search(r'key:"([^"]+)"', rec)
        name = re.search(r'name:"([^"]+)"', rec)
        if not key or num('lat') is None:
            continue
        locs.append({
            'key': key.group(1),
            'name': name.group(1) if name else key.group(1),
            'lat': num('lat'), 'lng': num('lng'),
            'hi': num('hi'), 'lo': num('lo'),
            'hs': num('hs'), 'ho': num('ho'),
            'ls': num('ls'), 'lo_': num('lo_'),
            'measured': 'measured:true' in rec.replace(' ', ''),
        })
    return locs


def dist_nm(lat1, lng1, lat2, lng2):
    dx = (lng1 - lng2) * math.cos(math.radians((lat1 + lat2) / 2))
    dy = lat1 - lat2
    return math.hypot(dx, dy) * NM_PER_DEG


def _interp_by_inlet_distance(anchors, d, fields):
    """estimateLag()'s shape: rank anchors by their own distance from the inlet,
    then linearly interpolate between the two that bracket d. Extrapolates off
    the last pair beyond the furthest anchor, exactly as the app does."""
    anchors = sorted(anchors, key=lambda a: a['dd'])
    if not anchors:
        return None
    if d <= anchors[0]['dd']:
        return {f: anchors[0][f] for f in fields}
    for a, b in zip(anchors, anchors[1:]):
        if a['dd'] <= d <= b['dd']:
            span = b['dd'] - a['dd']
            f = (d - a['dd']) / span if span > 0 else 0.0
            return {k: a[k] + (b[k] - a[k]) * f for k in fields}
    last, prev = anchors[-1], (anchors[-2] if len(anchors) > 1 else anchors[0])
    span = (last['dd'] - prev['dd']) or 1.0
    f = (d - last['dd']) / span
    return {k: last[k] + (last[k] - prev[k]) * f for k in fields}


class TideModel:
    """Lag + height scaling at an arbitrary position, then height at an instant."""

    def __init__(self, locs, preds):
        self.inlet = next(l for l in locs if l['key'] == 'inlet')
        self.preds = sorted(preds, key=lambda p: p['time'])
        self.lag_anchors = [
            {'dd': dist_nm(l['lat'], l['lng'], self.inlet['lat'], self.inlet['lng']),
             'hi': l['hi'], 'lo': l['lo']}
            for l in locs if l['hi'] is not None and l['lo'] is not None]
        # height scaling: MEASURED spots only. Guessing off an estimate would
        # be compounding a guess, and the board refuses to guess here at all.
        self.h_anchors = [
            {'dd': dist_nm(l['lat'], l['lng'], self.inlet['lat'], self.inlet['lng']),
             'hs': l['hs'], 'ho': l['ho'], 'ls': l['ls'], 'lo_': l['lo_']}
            for l in locs if l['measured'] and l['hs'] is not None]
        self.h_keys = [l['key'] for l in locs if l['measured'] and l['hs'] is not None]

    def params_at(self, lat, lng):
        d = dist_nm(lat, lng, self.inlet['lat'], self.inlet['lng'])
        lag = _interp_by_inlet_distance(self.lag_anchors, d, ('hi', 'lo'))
        sc = _interp_by_inlet_distance(self.h_anchors, d, ('hs', 'ho', 'ls', 'lo_'))
        return lag, sc, d

    def height_ft(self, lat, lng, t_local):
        """Tide height above MLLW at this position and instant, or None if the
        prediction set does not bracket it."""
        lag, sc, d = self.params_at(lat, lng)
        if not lag or not sc:
            return None, None
        # shifted(): each event moves by its own lag and is scaled by type
        ev = []
        for p in self.preds:
            is_h = p['type'] == 'H'
            ft = (sc['hs'] * p['ft'] + sc['ho']) if is_h else (sc['ls'] * p['ft'] + sc['lo_'])
            ev.append({'time': p['time'] + timedelta(minutes=(lag['hi'] if is_h else lag['lo'])),
                       'ft': ft, 'type': p['type']})
        ev.sort(key=lambda e: e['time'])
        # tideAt(): cosine between the bracketing events
        for a, b in zip(ev, ev[1:]):
            if a['time'] <= t_local <= b['time']:
                span = (b['time'] - a['time']).total_seconds()
                if span <= 0:
                    return None, None
                f = (t_local - a['time']).total_seconds() / span
                ft = a['ft'] + (b['ft'] - a['ft']) * (1 - math.cos(math.pi * f)) / 2
                return ft, d
        return None, d


# --------------------------------------------------------------------------
# NOAA predictions
# --------------------------------------------------------------------------

def fetch_preds(begin, end, station=STATION):
    """hi/lo predictions at MLLW, station local time. Cached on disk -- a track
    is reduced more than once while the thresholds are argued about, and NOAA
    should not be asked twice for the same fortnight."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    tag = '%s_%s_%s.json' % (station, begin.strftime('%Y%m%d'), end.strftime('%Y%m%d'))
    path = os.path.join(CACHE_DIR, tag)
    if os.path.exists(path):
        raw = json.load(open(path))
    else:
        url = ('https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
               '?product=predictions&application=tide_board_stage2'
               '&begin_date=%s&end_date=%s&datum=MLLW&station=%s'
               '&time_zone=lst_ldt&units=english&interval=hilo&format=json'
               % (begin.strftime('%Y%m%d'), end.strftime('%Y%m%d'), station))
        with urllib.request.urlopen(url, timeout=60) as r:
            raw = json.load(r)
        if 'predictions' not in raw:
            raise SystemExit('NOAA: %s' % raw.get('error', {}).get('message', raw))
        json.dump(raw, open(path, 'w'))
    out = []
    for p in raw['predictions']:
        t = datetime.strptime(p['t'], '%Y-%m-%d %H:%M').replace(tzinfo=LOCAL_TZ)
        out.append({'time': t, 'ft': float(p['v']), 'type': p['type']})
    return out


# --------------------------------------------------------------------------
# GPX
# --------------------------------------------------------------------------

def local_name(tag):
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def parse_tracks(path):
    """-> [{'name': str, 'points': [{'lat','lng','t','depth_m'}]}]. Depth is
    matched on any tag whose LOCAL name is 'depth' at any nesting, because the
    namespace moved between firmware generations (stage 1 learned this)."""
    root = ET.parse(path).getroot()
    tracks = []
    for trk in root.iter():
        if local_name(trk.tag) != 'trk':
            continue
        nm = None
        for ch in trk:
            if local_name(ch.tag) == 'name':
                nm = (ch.text or '').strip()
                break
        pts = []
        for seg in trk:
            if local_name(seg.tag) != 'trkseg':
                continue
            for tp in seg:
                if local_name(tp.tag) != 'trkpt':
                    continue
                lat, lng = tp.get('lat'), tp.get('lon')
                if lat is None or lng is None:
                    continue
                t, depth = None, None
                for node in tp.iter():
                    ln = local_name(node.tag).lower()
                    if ln == 'time' and node.text:
                        s = node.text.strip().replace('Z', '+00:00')
                        try:
                            t = datetime.fromisoformat(s)
                        except ValueError:
                            t = None
                    elif 'depth' in ln and node.text:
                        try:
                            depth = float(node.text)
                        except ValueError:
                            pass
                if t is not None and t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                pts.append({'lat': float(lat), 'lng': float(lng), 't': t, 'depth_m': depth})
        if pts:
            tracks.append({'name': nm or '(unnamed)', 'points': pts})
    return tracks


CSV_ALIASES = {
    'lat': ('lat', 'latitude', 'y'),
    'lng': ('lng', 'lon', 'long', 'longitude', 'x'),
    't': ('time_iso', 'time', 'timestamp', 'datetime', 'utc', 'date_time'),
    'depth': ('reading_ft', 'depth_ft', 'depth', 'sounding_ft', 'sounding', 'depth_m'),
    'offset': ('offset_ft', 'offset', 'transducer_ft'),
    'name': ('name', 'spot', 'label'),
}


def parse_csv(path):
    """Any four-column log of soundings, whatever wrote it.

    This is the door for a different boat, a different sounder, a castable, or
    a lead line -- none of which speak GPX. The board's own Survey panel writes
    this exact shape, so a phone log round-trips through here unchanged.

    Column names are matched loosely (lat/latitude/y, time/time_iso/timestamp,
    ...), because the point is to accept what the other instrument already
    emits rather than make anyone rename columns on a boat. Depth is assumed
    FEET unless the column is named depth_m -- the opposite of the GPX default,
    and deliberately so: a person typing a number types the units they read,
    while a machine writes the units its schema mandates.

    A timestamp with no zone is read as LOCAL time, not UTC. Getting that
    backwards is a 4-hour error, which at Crook's Corner is the entire 2.2 ft
    tide range -- so a bare timestamp is reported, not silently assumed.
    """
    rows, bare_tz = [], 0
    with open(path, newline='') as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames:
            return [], 0
        cols = {}
        low = {(c or '').strip().lower(): c for c in rd.fieldnames}
        for want, names in CSV_ALIASES.items():
            for n in names:
                if n in low:
                    cols[want] = low[n]
                    break
        for need in ('lat', 'lng', 't', 'depth'):
            if need not in cols:
                raise SystemExit('%s: no column for %s (looked for %s; found %s)'
                                 % (path, need, '/'.join(CSV_ALIASES[need]),
                                    ', '.join(rd.fieldnames)))
        in_metres = cols['depth'].strip().lower() == 'depth_m'
        for r in rd:
            try:
                lat, lng = float(r[cols['lat']]), float(r[cols['lng']])
                depth = float(r[cols['depth']])
            except (TypeError, ValueError):
                continue
            raw = (r[cols['t']] or '').strip().replace('Z', '+00:00').replace(' ', 'T', 1)
            try:
                t = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=LOCAL_TZ)
                bare_tz += 1
            off = 0.0
            if 'offset' in cols:
                try:
                    off = float(r[cols['offset']])
                except (TypeError, ValueError):
                    off = 0.0
            rows.append({'lat': lat, 'lng': lng, 't': t,
                         'depth_m': (depth if in_metres else depth / M_TO_FT) + off / M_TO_FT,
                         'name': (r.get(cols['name']) or '').strip() if 'name' in cols else ''})
    return rows, bare_tz


def cluster(rows, radius_yd):
    """Group unnamed soundings that sit on top of each other.

    Jan surveys water with no card and often no charted name, so bucketing
    against the board's spots would throw away every point that matters. Points
    already carrying a name group by name; the rest group by proximity --
    greedy, seeded on the first ungrouped point, which is enough for a handful
    of drifts over one hole and does not pretend to be a clustering algorithm.
    """
    named, out = {}, []
    loose = []
    for r in rows:
        if r.get('name'):
            named.setdefault(r['name'], []).append(r)
        else:
            loose.append(r)
    for name, pts in named.items():
        out.append((name, pts))
    while loose:
        seed = loose.pop(0)
        grp = [seed]
        rest = []
        for r in loose:
            if dist_nm(seed['lat'], seed['lng'], r['lat'], r['lng']) * 2025.37 <= radius_yd:
                grp.append(r)
            else:
                rest.append(r)
        loose = rest
        out.append((None, grp))
    return out


def dedupe(points, seen):
    """Drop pings already contributed by an earlier file.

    This is not a nicety. Every ACTIVE LOG export is the WHOLE rolling buffer,
    not the part you have not seen yet, so two exports a month apart overlap by
    nearly everything they both contain. Reduce both and the overlap counts
    twice: n doubles, and a median weighted toward whatever the boat did in the
    shared stretch stops describing the spot.

    Identity is (time, position) -- one ping of the sounder. Depth is
    deliberately NOT in the key: if the same instant at the same place carries
    two different depths, that is contradictory data, and silently keeping both
    is the one outcome with nothing to recommend it.
    """
    out = 0
    keep = []
    for p in points:
        if p['t'] is None:
            keep.append(p)
            continue
        k = (p['t'].timestamp(), round(p['lat'], 6), round(p['lng'], 6))
        if k in seen:
            out += 1
            continue
        seen.add(k)
        keep.append(p)
    return keep, out


def split_runs(points, gap_min=60):
    """One saved track can hold several outings; a >1 h gap is a new run. Keeps
    a four-month-old track from being averaged into today's."""
    runs, cur = [], []
    for p in points:
        if cur and p['t'] and cur[-1]['t'] and \
                (p['t'] - cur[-1]['t']).total_seconds() > gap_min * 60:
            runs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        runs.append(cur)
    return runs


# --------------------------------------------------------------------------
# reduction
# --------------------------------------------------------------------------

def reduce_points(points, model, offset_mode='waterline', offset_ft=1.0):
    """-> rows with sounder depth, tide, and depth at MLLW."""
    rows = []
    for p in points:
        if p['depth_m'] is None or p['depth_m'] <= 0 or p['t'] is None:
            continue          # 0.00 is the sounder off bottom, not a shoal
        raw_ft = p['depth_m'] * M_TO_FT
        # a positive Garmin offset is transducer->waterline and is already IN
        # the exported number; a negative one means the reading is below-keel
        # and the offset has to be added back to reach the surface
        to_waterline = raw_ft if offset_mode == 'waterline' else raw_ft + abs(offset_ft)
        t_local = p['t'].astimezone(LOCAL_TZ)
        tide, d_inlet = model.height_ft(p['lat'], p['lng'], t_local)
        if tide is None:
            continue
        rows.append({
            'lat': p['lat'], 'lng': p['lng'], 't_local': t_local,
            'sounder_ft': to_waterline, 'tide_ft': tide,
            'mllw_ft': to_waterline - tide, 'inlet_nm': d_inlet,
        })
    return rows


def nearest_spot(lat, lng, locs, radius_yd):
    best, bd = None, None
    for l in locs:
        d = dist_nm(lat, lng, l['lat'], l['lng']) * 2025.37   # nm -> yards
        if d <= radius_yd and (bd is None or d < bd):
            best, bd = l, d
    return best, bd


def fmt_spread(vals):
    if len(vals) == 1:
        return '%.1f' % vals[0]
    s = sorted(vals)
    return '%.1f-%.1f' % (s[0], s[-1])


def main(argv):
    # every flag here takes a value, so a flag consumes the token after it —
    # otherwise the output path lands in the file list and gets parsed as GPX
    TAKES_VALUE = {'--radius', '--offset', '--offset-mode', '--csv'}
    opts, paths, skip = {}, [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in TAKES_VALUE:
            opts[a] = argv[i + 1] if i + 1 < len(argv) else None
            skip = True
        elif a.startswith('--'):
            opts[a] = True
        else:
            paths.append(a)
    def opt(name, default=None):
        v = opts.get(name)
        return default if v is None or v is True else v
    radius = float(opt('--radius', DEFAULT_RADIUS_YD))
    offset_ft = float(opt('--offset', 1.0))
    offset_mode = opt('--offset-mode', 'waterline')
    csv_path = opt('--csv')
    if not paths:
        print(__doc__)
        return 1

    locs = load_locations()
    tracks, csv_rows, bare_tz = [], [], 0
    seen, dropped = set(), 0
    # oldest file first, so the earliest export owns a ping and later
    # re-exports of the same buffer are the ones trimmed
    for p in sorted(paths, key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0):
        if p.lower().endswith('.csv'):
            rows, bare = parse_csv(p)
            rows, d = dedupe(rows, seen)
            dropped += d
            for r in rows:
                r['file'] = os.path.basename(p)
            csv_rows.extend(rows)
            bare_tz += bare
        else:
            for t in parse_tracks(p):
                t['points'], d = dedupe(t['points'], seen)
                dropped += d
                if not t['points']:
                    continue
                t['file'] = os.path.basename(p)
                tracks.append(t)
    if dropped:
        print('%d duplicate ping%s dropped — the same time and position seen in an '
              'earlier file.\n   Expected when exports overlap: each ACTIVE LOG export '
              'is the whole buffer.' % (dropped, '' if dropped == 1 else 's'))
    if not tracks and not csv_rows:
        print('no tracks or CSV soundings found')
        return 1

    all_times = ([pt['t'] for t in tracks for pt in t['points'] if pt['t']]
                 + [r['t'] for r in csv_rows])
    begin = min(all_times).astimezone(LOCAL_TZ) - timedelta(days=2)
    end = max(all_times).astimezone(LOCAL_TZ) + timedelta(days=2)
    print('NOAA %s predictions %s -> %s' %
          (STATION, begin.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')))
    model = TideModel(locs, fetch_preds(begin, end))
    print('height scaling interpolated between measured anchors: %s'
          % ', '.join(model.h_keys))
    if tracks:
        print('GPX transducer offset: %+.1f ft, treated as %s' % (offset_ft, offset_mode))
    if csv_rows:
        print('CSV soundings carry their own per-row offset; --offset does not touch them')
    print('')

    all_rows = []
    for t in tracks:
        runs = split_runs(t['points'])
        for ri, run in enumerate(runs, 1):
            rows = reduce_points(run, model, offset_mode, offset_ft)
            if not rows:
                continue
            for r in rows:
                r['track'] = '%s/%s' % (t['file'], t['name'])
                r['run'] = ri
            all_rows.extend(rows)
            t0, t1 = rows[0]['t_local'], rows[-1]['t_local']
            print('%-28s run %d  %s -> %s  %4d soundings  tide %+.2f to %+.2f ft'
                  % (t['name'], ri, t0.strftime('%Y-%m-%d %H:%M'), t1.strftime('%H:%M'),
                     len(rows), min(r['tide_ft'] for r in rows),
                     max(r['tide_ft'] for r in rows)))

    # ---- CSV soundings: grouped by their own name or by proximity ----
    if csv_rows:
        if bare_tz:
            print('\n%d CSV timestamp%s carried no timezone — read as %s local. If that '
                  'log\n   was written in UTC these are wrong by the offset; at Crook\'s Corner\n'
                  '   4 hours is the whole 2.2 ft tide range.'
                  % (bare_tz, '' if bare_tz == 1 else 's', LOCAL_TZ.key))
        print('\n%s' % ('=' * 78))
        print('CSV SOUNDINGS  (depth at MLLW, grouped by name or within %g yd)' % radius)
        print('=' * 78)
        for name, pts in cluster(csv_rows, radius):
            red = reduce_points(pts, model)          # offsets already folded in
            for r in red:
                r['track'] = '%s/%s' % (pts[0].get('file', 'csv'), name or 'cluster')
                r['run'] = 0
            all_rows.extend(red)
            if not red:
                print('\n  %-26s %d point(s), none reducible' % (name or '(unnamed)', len(pts)))
                continue
            mllw = [r['mllw_ft'] for r in red]
            med = statistics.median(mllw)
            spread = max(mllw) - min(mllw)
            lat = statistics.median([r['lat'] for r in red])
            lng = statistics.median([r['lng'] for r in red])
            print('\n  %-26s n=%-4d  median %5.1f ft MLLW   spread %4.1f ft'
                  % (name or '(unnamed)', len(red), med, spread))
            print('     at %.5f %.5f   tide applied %+.2f to %+.2f ft'
                  % (lat, lng, min(r['tide_ft'] for r in red), max(r['tide_ft'] for r in red)))
            if survey_depth_at:
                s = survey_depth_at(lat, lng)
                if s and s['near_m'] <= 600:
                    print('     survey says %5.1f ft MLLW  ->  sounded is %+.1f ft vs survey'
                          % (s['mllw'], med - s['mllw']))
                else:
                    print('     survey has nothing to say here '
                          '(no contour within 600 m) — which is often the point')
            if spread > 3:
                print('     SPREAD > 3 ft: a slope, not a depth. Split it or drop the pass.')

    if not all_rows:
        print('nothing reducible (no depth, or outside the prediction window)')
        return 1

    # ---- candidates per board spot ----
    buckets = {}
    for r in all_rows:
        sp, d = nearest_spot(r['lat'], r['lng'], locs, radius)
        if sp:
            buckets.setdefault(sp['key'], {'spot': sp, 'rows': []})['rows'].append(r)

    print('\n%s' % ('=' * 78))
    print('soundedFt CANDIDATES  (depth at MLLW, within %g yd of a board spot)' % radius)
    print('=' * 78)
    if not buckets:
        print('  no trackpoint came within %g yd of a board spot.' % radius)
    for key, b in sorted(buckets.items(), key=lambda kv: -len(kv[1]['rows'])):
        rows = b['rows']
        mllw = [r['mllw_ft'] for r in rows]
        med = statistics.median(mllw)
        spread = max(mllw) - min(mllw)
        sp = b['spot']
        est = '' if sp.get('measured') else '  est'
        print('\n  %-26s n=%-4d  median %5.1f ft MLLW   spread %4.1f ft%s'
              % (sp['name'], len(rows), med, spread, est))
        print('     raw sounder %s ft   tide applied %+.2f to %+.2f ft'
              % (fmt_spread([r['sounder_ft'] for r in rows]),
                 min(r['tide_ft'] for r in rows), max(r['tide_ft'] for r in rows)))
        if survey_depth_at:
            s = survey_depth_at(sp['lat'], sp['lng'])
            if s:
                print('     survey says %5.1f ft MLLW  ->  sounded is %+.1f ft vs survey'
                      % (s['mllw'], med - s['mllw']))
            else:
                print('     survey has no contour within 1.3 km')
        if spread > 3:
            print('     SPREAD > 3 ft: this is not one depth. Check the track ran '
                  'over a slope,\n     not a hole, before putting a single number on it.')

    # ---- coverage: where the boat has NOT been ----
    print('\n%s' % ('=' * 78))
    print('COVERAGE  (nearest approach of any sounding to each board spot)')
    print('=' * 78)
    for l in sorted(locs, key=lambda x: x['lat'], reverse=True):
        near = min((dist_nm(r['lat'], r['lng'], l['lat'], l['lng']) * 2025.37
                    for r in all_rows), default=None)
        if near is None:
            continue
        n_in = len(buckets.get(l['key'], {}).get('rows', []))
        if n_in:
            print('  %-26s %4d soundings inside %g yd' % (l['name'], n_in, radius))
        elif near < 1760:
            print('  %-26s   --   nearest sounding %4.0f yd away' % (l['name'], near))
        else:
            print('  %-26s   --   nearest sounding %4.1f miles away'
                  % (l['name'], near / 1760))

    if csv_path:
        with open(csv_path, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['track', 'run', 'time_local', 'lat', 'lng',
                        'sounder_ft_to_waterline', 'tide_ft_MLLW', 'depth_ft_MLLW',
                        'inlet_nm'])
            for r in all_rows:
                w.writerow([r['track'], r['run'], r['t_local'].strftime('%Y-%m-%d %H:%M:%S'),
                            '%.6f' % r['lat'], '%.6f' % r['lng'],
                            '%.2f' % r['sounder_ft'], '%.2f' % r['tide_ft'],
                            '%.2f' % r['mllw_ft'], '%.2f' % r['inlet_nm']])
        print('\n%d reduced soundings -> %s' % (len(all_rows), csv_path))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
