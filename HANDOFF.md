# Handing this app to someone else

Everything the board needs to RUN is in this repo. What follows is the short
list of things that are not, and the two lines that have to change if the app
moves to a different GitHub account.

This is deliberately a much shorter list than WTF's. There are no API keys, no
scheduled agents on a personal account, and no emailing workflow.

---

## 1. The Cloudflare worker

`worker/ndbc-proxy.js` gets buoy 41070's readings past the browser's CORS rule.
It is currently deployed on **Jan's** Cloudflare account at
`https://ndbc-proxy.janfishes.workers.dev`, and `BUOY_PROXY` in `index.html`
points at it.

**It does not transfer with the repo.** But unlike a secret, it does not need
to: the whole source is here and it holds nothing private. A new owner runs

```sh
cd worker
npx wrangler login      # their own Cloudflare account, free, no card
npx wrangler deploy
```

and gets their own URL back in about a minute.

Then **two lines change**:

1. `BUOY_PROXY` in `index.html` → their worker URL.
2. `ALLOWED_ORIGINS` in `worker/ndbc-proxy.js` → their GitHub Pages origin.

**Miss the second one and the buoy dies quietly.** The worker only answers
`https://janfishes.github.io`; from any other origin the browser blocks the
response. The board does not crash — it falls back to the public relays, and
when those fail (they all did on 2026-08-05) it falls back again to assuming
the sea follows the wind. The inlet card keeps working and keeps looking
right. It just stops using measured wave direction, and says so in small type
at the bottom of the card. That is the failure worth knowing about, because
nothing about it is loud.

The alternative is to leave it pointed at Jan's worker and add the new owner's
origin to `ALLOWED_ORIGINS`. That works, costs nothing (free tier is 100,000
requests a day against this app's few hundred), and means one less account for
them to hold — but it also means the app depends on an account they do not
control, which is the situation this worker was built to get out of.

## 2. The Pages URL moves, and that breaks installed copies

Transferring the repo moves the site from `janfishes.github.io/tideboard/` to
`<newowner>.github.io/tideboard/`. Anyone who has added the board to a phone
home screen has that old URL baked into their device's service-worker cache;
their icon keeps opening the old address, which will 404 once the repo moves.

Adding the new owner as a **collaborator** instead keeps the URL and grants
push access. Recommended order: collaborator first, transfer only if and when
ownership really has to change. (Same reasoning, same trap, as WTF.)

## 3. Repo settings that do not come across automatically

- **Actions must be allowed to write.** `.github/workflows/refresh-tides.yml`
  commits the regenerated tide table back to the repo. New repos default the
  workflow token to read-only, and the job fails at the push step. Settings →
  Actions → General → Workflow permissions → **Read and write**.
- **Pages must be switched on** for the new repo: Settings → Pages → source
  `main`, folder `/`.

## 4. What is on Jan's Desktop and not in git

- `~/Desktop/Tide Board Files/` — the icon master artwork and the SVG it was
  drawn from. The three PNGs in the repo are resampled from the 2048.
- `~/Desktop/djm1.png`, `djm2.png` — the photographed DJM tide sheet the
  gauge calibration was derived from. The *result* is baked into
  `BUILTIN_LOCATIONS`, so the app does not need them, but they are the only
  record of where those numbers came from and the two photos disagree in one
  row. Worth keeping with the app.

## 5. What nobody can hand over

Every user's calibrated lags, sounded depths and added spots live in that
device's `localStorage`. There is nothing to transfer and nothing to back up
centrally — by design.

---

## The short version

Deploy a worker, change two lines, turn on Pages and Actions write. Half an
hour, no secrets, nothing to buy.
