# Tide Board — Halifax River & Ponce de Leon Inlet

A single-file web app for the water off Volusia County, Florida: tides at six
spots up the Halifax, the depth of water standing on the bottom right now,
solunar feeding times, and **inlet run windows** — the stretches of the day when
the current through Ponce and the sea outside it are *not* fighting each other.

Live at `https://janfishes.github.io/tides/` (once Pages is switched on).

---

## What it does

**Tides.** NOAA station 8721138 (Halifax River, Ponce Inlet) high/low predictions
for seven days, shifted upriver by a per-spot lag you can calibrate by watching
one real turn. A year-plus of official predictions is embedded in the page, so
the board works with no signal at all.

**Water now.** The surveyed bottom plus the tide standing on it. Depths come
from the same NOAA BlueTopo / NCEI CUDEM contour blocks the WTF chart draws,
converted from NAVD88 to actual water using the measured 2.25 ft offset between
NAVD88 and MLLW at Ponce.

**Feeding times.** Solunar majors and minors from moon transit, moonrise and
moonset, with the phase.

**Running the inlet.** The headline. A wave meeting an opposing current gets
shorter, steeper and taller — that is what stands the Ponce bar up, and it is
why an ebb with an east sea breaks when the same sea on the flood does not. The
board walks the day in twenty-minute steps, works out how hard the current is
running and which way the sea is going, and marks the stretches where the two
are not opposed.

---

## The physics behind the inlet card

Linear wave theory on a current. With deep-water phase speed `c0 = gT/2π` and
`U` the current component along the wave's direction of travel (negative when
opposing):

```
s = sqrt(1 + 4U/c0)          H/H0 = sqrt( 2 / (s·(1+s)) )
```

`s = 1` and `H/H0 = 1` in still water; height runs away as `s → 0`, the blocking
point at `U = −c0/4`. A 7-second sea has `c0 ≈ 21 kt`, so blocking is around
5 kt — more than Ponce runs — but a 3.5 kt ebb still puts `s ≈ 0.58` and
multiplies the height by about 1.5, steepening it far more than that.

Ratings are thresholds on the **steepened** height, deliberately conservative,
plus a floor: a hard opposing current downgrades the rating on its own even
under a small sea, because confused standing water is what catches people out
and significant wave height does not describe it.

### Where the numbers come from, and what is assumed

| Input | Source | Honest limits |
|---|---|---|
| Tide height & rate | NOAA 8721138 predictions | Astronomical only — wind and runoff move real water |
| Current speed | **Derived** from the rate of rise/fall, scaled to a calibratable peak (default 3.5 kt) | NOAA publishes **no** current prediction anywhere near Ponce — the whole station list was checked. This is a model, not an observation. Tap "peak ebb" to calibrate. |
| Current direction | Fixed: ebb 105°T, flood 285°T | The channel heading past the jetties |
| Wind speed & direction | NWS gridpoint `MLB/42,94`, hourly, ~7 days | Direct fetch, CORS-clean, no proxy |
| Wave height & period | Same NWS gridpoint | Coarse — a single value can span 2½ days |
| **Wave direction** | NDBC buoy 41070, **observed only** | **NWS publishes no wave-direction forecast for this point.** Past the buoy's reading the board assumes the sea follows the forecast wind — right for a local chop, wrong for a ground swell. Every window says which it used. |

The buoy comes in through a public CORS relay and the last good reading is kept
on the device, because a swell does not swing round in an hour and "the sea
follows the wind" is wrong exactly when it matters most: an east ground swell
under a light west wind is the classic Ponce trap.

---

## Files

```
index.html                    the whole app — no build step, no CDN
sw.js                         stale-while-revalidate shell cache
manifest.json                 installable to the home screen
depth/c*.json                 6 contour blocks copied from the WTF repo
tools/refresh_fallback.py     regenerates the embedded NOAA table
.github/workflows/            monthly gate that runs the refresh when due
```

### Depth blocks

Copied from `~/Documents/WTF/depth/`, covering 28.5–29.48 N, 81.2–80.75 W —
Mosquito Lagoon up to Bulow. Format is `[ft, tier, x0, y0, dx, dy, …]` in
hundred-thousandths of a degree from each block's SW corner. WTF *draws* these
lines; this board *reads a number off them* at a point, by interpolating between
the two nearest contour levels by distance. That is exactly as good as the
contour interval and no better, which is why the card prints a `~` and a band.

If the blocks are ever rebuilt in WTF, re-copy them **and** bump `DEPTH_DATA_V`
in `index.html` — the in-page cache is cache-first and will otherwise serve the
old blocks forever.

---

## Maintenance

**The embedded tide table refreshes itself.** `.github/workflows/refresh-tides.yml`
runs monthly, asks `tools/refresh_fallback.py --check` whether the table has
under 300 days left, and only then regenerates, bumps `BUILD_NUM` and commits.
Manual run:

```sh
python3 tools/refresh_fallback.py --months 14   # regenerate now
python3 tools/refresh_fallback.py --check       # exit 3 if a refresh is due
```

**A new build always takes two opens.** `sw.js` is stale-while-revalidate: the
first open serves the saved copy, the refetch lands for the next. That is by
design, not a failed deploy. Check what is actually live before diagnosing:

```sh
curl -s https://janfishes.github.io/tides/ | grep -o "BUILD_NUM = [0-9]*"
```

The update pill closes that gap on demand — its check is not a navigation, so it
goes straight to the network.

**Local preview:**

```sh
cd ~/Documents/tides && python3 -m http.server 8799
# http://localhost:8799/index.html
```

The service worker and the yearly refresh both need real hosting — neither works
from a `file://` double-click.

---

## Known gaps

- **Crook's Corner and DJM have no coordinates.** They are Jan's own names and
  appear on no chart and in no gazetteer, so there was nothing to look them up
  in. Their tide *times* work exactly as before — a lag is all a time needs —
  but they cannot show a depth until they have a position. Tap "set position" on
  either card and pick the nearest waypoint, or put real coordinates into
  `BUILTIN_LOCATIONS` in `index.html`.
- **Icons are WTF's, as placeholders.** The board needs its own artwork.
- **No iPhone launch images.** WTF has eleven; without them a home-screen icon
  opens to a white screen for a second or two. Same fix if it becomes annoying:
  screenshot the first paint at each device size into `launch/` and add the
  media-query link table.
- Wave direction beyond the buoy reading is an assumption, not a forecast — see
  the table above.

---

## Credit and caution

Tide, depth, wind and buoy data courtesy NOAA — NOS/CO-OPS, NCEI, the National
Weather Service and the National Data Buoy Center. Astronomy is a trimmed port
of SunCalc 1.9.0 © 2014 Vladimir Agafonkin, BSD-2-Clause, inlined so the board
has no runtime dependency on any CDN.

**The inlet ratings are a rough guide built from forecasts.** They are not an
observation of the bar and not a substitute for looking at it. Conditions at
Ponce change with the sandbar, with a squall, and with a swell the forecast
never saw. Never run this inlet on this app alone.

Developed by Jan G. Neal, 2026.
