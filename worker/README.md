# NDBC proxy

A ~40-line Cloudflare Worker that fetches buoy 41070's readings and returns
them with a CORS header, so the board can read them from the browser.

## Why

NDBC sends no `Access-Control-Allow-Origin`, so a browser blocks a direct
fetch. The board used to route around that through free public CORS relays.
Measured from the live site on 2026-08-05, **all five failed** — allorigins
timed out at 19.7 s, corsproxy.io returned 403, codetabs / cors.lol /
isomorphic-git were blocked outright. They gate on the calling origin and
change without notice.

41070 is the only source of measured **wave direction** on this coast, and wave
direction against the current is the entire basis of the inlet run windows.
Without it the board assumes the sea follows the wind — fine for a summer chop,
wrong for an east ground swell under a light west wind, which is the Ponce trap.
That makes it the one dependency worth owning rather than borrowing.

## Deploy

```sh
cd worker
npx wrangler login      # opens a browser, one time
npx wrangler deploy
```

Wrangler prints a URL like `https://ndbc-proxy.janfishes.workers.dev`.
Put it into `BUOY_PROXY` near the top of the buoy section in `../index.html`:

```js
const BUOY_PROXY = "https://ndbc-proxy.janfishes.workers.dev";
```

Commit, push, and the board will use it first and fall back to the old public
relays only if it is unreachable.

## Check it

```sh
curl -s "https://ndbc-proxy.<you>.workers.dev/?station=41070" | head -3
curl -sI "https://ndbc-proxy.<you>.workers.dev/?station=41070" | grep -i "access-control\|x-cache"
```

You should get the `#YY MM DD hh mm WDIR ...` header, an
`access-control-allow-origin` header, and `X-Cache: MISS` then `HIT`.

## Cost

Free tier is 100,000 requests/day. With a ten-minute cache this app makes at
most a few hundred. There is no realistic path to a bill.

## Notes

- Holds **no secrets**, so nothing here needs rotating if the repo changes
  hands — unlike `RESEND_API_KEY` in WTF. A new owner runs the two commands
  above under their own account and updates `BUOY_PROXY`.
- `ALLOWED_ORIGINS` in `ndbc-proxy.js` is deliberately short. An open proxy is
  an invitation to have your quota spent by strangers. Add an origin there if
  the board ever moves off `janfishes.github.io`.
- The ten-minute cache is politeness as much as speed: 41070 reports every
  30–60 minutes, so anything shorter just hammers a public service for readings
  that have not changed.
- **WTF has this same broken relay chain** behind its Waves button. Pointing it
  at this worker would fix it too — same URL, same station parameter.
