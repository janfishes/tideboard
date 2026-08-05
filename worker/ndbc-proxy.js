/* NDBC proxy — a Cloudflare Worker.
 *
 * WHY THIS EXISTS
 * ---------------
 * NDBC serves buoy 41070's readings over plain HTTP with no
 * Access-Control-Allow-Origin header, so a browser will not let the board read
 * it directly. Both this app and WTF worked around that with a chain of free
 * public CORS relays — corsproxy.io, allorigins, codetabs. Measured from the
 * live site on 2026-08-05, ALL of them failed: allorigins timed out at 19.7 s,
 * corsproxy returned 403, the rest were blocked outright. They gate on the
 * calling origin and change their minds without warning, which is exactly the
 * behaviour you cannot build a boat tool on.
 *
 * Buoy 41070 is the only source of measured WAVE DIRECTION for this coast, and
 * wave direction against the current is the whole basis of the inlet run
 * windows. Without it the board falls back to assuming the sea follows the
 * wind — right for a summer chop, wrong for an east ground swell under a light
 * west wind, which is the classic Ponce trap. So this is the one dependency
 * worth owning outright rather than borrowing.
 *
 * WHAT IT DOES
 * ------------
 * Fetches the NDBC realtime2 text file for a station and returns it with a
 * CORS header, cached for ten minutes. Nothing else. It does not parse, store
 * or log anything, and it holds no secrets — so there is nothing here that
 * needs rotating if the repo changes hands.
 *
 * Ten minutes of cache is not only for speed: 41070 only reports every 30-60
 * minutes, so anything shorter is just hammering a public service for readings
 * that have not changed. Being a good citizen of NDBC's bandwidth is part of
 * the point of running our own proxy rather than pushing the load onto someone
 * else's free relay.
 *
 * DEPLOY
 * ------
 *   cd worker && npx wrangler login && npx wrangler deploy
 * then put the printed https://ndbc-proxy.<you>.workers.dev URL into
 * BUOY_PROXY in index.html. See worker/README.md.
 */

const NDBC_BASE = 'https://www.ndbc.noaa.gov/data/realtime2/';
const CACHE_SECONDS = 600;

/* Origins allowed to call this. Keep it short: an open proxy is an invitation
 * to have your quota spent by strangers. The first entry is the fallback used
 * when a request arrives with no Origin at all (curl, a health check). */
const ALLOWED_ORIGINS = [
  'https://janfishes.github.io',   // serves BOTH the tide board and WTF — an origin
];                                 // is scheme+host, so the /WTF/ path is irrelevant

/* Any localhost port, for previewing either app with `python3 -m http.server`.
 * The two use different ports and pinning them here meant the worker rejected
 * whichever one was not listed — which is how WTF's first test failed. A
 * loopback origin cannot be reached by anyone else's browser, and the payload
 * is public NOAA data either way, so this costs nothing. */
const LOCALHOST = /^http:\/\/(localhost|127\.0\.0\.1):\d+$/;

function corsHeaders(request) {
  const origin = request.headers.get('Origin') || '';
  const ok = ALLOWED_ORIGINS.includes(origin) || LOCALHOST.test(origin);
  const allow = ok ? origin : ALLOWED_ORIGINS[0];
  return {
    'Access-Control-Allow-Origin': allow,
    'Vary': 'Origin',
    'Cache-Control': `public, max-age=${CACHE_SECONDS}`,
  };
}

export default {
  async fetch(request, env, ctx) {
    const cors = corsHeaders(request);

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: { ...cors, 'Access-Control-Allow-Methods': 'GET, OPTIONS' },
      });
    }
    if (request.method !== 'GET') {
      return new Response('GET only', { status: 405, headers: cors });
    }

    // Station ids are five alphanumerics (41070, PCBF1). Validating rather than
    // interpolating whatever arrives is what stops this being a general-purpose
    // open relay pointed at the rest of the internet.
    const id = (new URL(request.url).searchParams.get('station') || '41070').toUpperCase();
    if (!/^[A-Z0-9]{5}$/.test(id)) {
      return new Response('bad station id', { status: 400, headers: cors });
    }

    const upstream = NDBC_BASE + id + '.txt';
    const cache = caches.default;
    const cacheKey = new Request(upstream, { method: 'GET' });

    const hit = await cache.match(cacheKey);
    if (hit) {
      return new Response(hit.body, {
        status: 200,
        headers: { ...cors, 'Content-Type': 'text/plain; charset=utf-8', 'X-Cache': 'HIT' },
      });
    }

    let res;
    try {
      res = await fetch(upstream, {
        headers: { 'User-Agent': 'tide-board (jan@aceshardware.com)' },
        cf: { cacheTtl: CACHE_SECONDS, cacheEverything: true },
      });
    } catch (e) {
      return new Response('upstream unreachable', { status: 502, headers: cors });
    }
    if (!res.ok) {
      return new Response('upstream ' + res.status, { status: 502, headers: cors });
    }

    const body = await res.text();
    // Refuse to cache or serve junk. A realtime2 file starts with a '#YY MM DD'
    // header and carries thousands of rows; anything short or not starting with
    // '#' is an error page wearing a 200, and caching that for ten minutes would
    // turn a blip into a ten-minute outage.
    if (body.length < 200 || body[0] !== '#') {
      return new Response('upstream returned nothing usable', { status: 502, headers: cors });
    }

    ctx.waitUntil(cache.put(cacheKey, new Response(body, {
      status: 200,
      headers: {
        'Content-Type': 'text/plain; charset=utf-8',
        'Cache-Control': `public, max-age=${CACHE_SECONDS}`,
      },
    })));

    return new Response(body, {
      status: 200,
      headers: { ...cors, 'Content-Type': 'text/plain; charset=utf-8', 'X-Cache': 'MISS' },
    });
  },
};
