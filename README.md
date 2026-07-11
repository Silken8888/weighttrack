# WeighTrack

Personal weight-loss/health tracker, companion to WannaPeek. Flask + SQLAlchemy,
same DigitalOcean setup (App Platform + Managed Postgres).

## What's in this drop

- **Redesign**: dark navy dashboard shell (coral/teal/pink accents on
  `#0f1420`/`#1b2333` surfaces), rounded cards throughout, Sora + Inter
  type. Replaced an earlier kraft-paper direction and, before that, a
  vintage pantry-label look -- both scrapped per feedback.
- **Title Case on every label/badge/stat name** -- tags, nav items, stat
  card labels, and Nutrition Facts row names. Headlines and body copy stay
  sentence case.
- **Today's Timeline**: a new `FoodLogEntry` model + `/log/add` /
  `/log/<id>/delete` routes. Log any library item to breakfast / lunch /
  dinner / snack with a servings multiplier; the timeline shows everything
  logged today in order, color-coded by meal, with a running "Calories
  Today" stat computed from real entries (not a placeholder).
- **Photo-based product lookup**: `POST /food/search-photo` accepts an
  uploaded photo and tries, in order:
  1. **Barcode decode** (`pyzbar`, local, no network) -- if a UPC/EAN is
     readable, it's an exact lookup via Open Food Facts'
     `/api/v2/product/<barcode>.json`. This is the reliable path.
  2. **OCR fallback** (`pytesseract`) when no barcode is found -- reads
     text off the label, takes the most likely product-name line, and
     feeds it through the same search path as a typed query.
  
  Both are honest about a real limitation: **Open Food Facts has no public
  reverse-image / visual search API**, so this isn't "match this exact
  photo" -- it's barcode-first with OCR-text as a fallback, which is what's
  actually achievable against their API today.

## What I actually tested against the live Open Food Facts API

Not just unit tests against mocks -- these ran against the real API with
the exact product from the photo you shared (Starbucks Caramel Macchiato
Non-Dairy Creamer, barcode `0050000993345`):

- Typing the **full label text verbatim**, including the size
  (`"Starbucks Caramel Macchiato Non-Dairy Creamer, 28 fl. oz."`), returned
  **0 results** -- confirmed live. Turns out `"Non-Dairy"` isn't in the
  field Open Food Facts matches against, so it kills the match even after
  stripping the size.
- Fixed with two changes to the search path, both validated end-to-end
  through the real Flask routes against the real API:
  - `_clean_query()` strips trailing size/quantity phrases (28 fl. oz.,
    12 oz, 500g, etc.) via regex.
  - `_progressive_search()` retries with progressively fewer trailing
    words if the full query comes back empty. For this product:
    `"Starbucks Caramel Macchiato Non-Dairy Creamer"` -> 0 results,
    `"Starbucks Caramel Macchiato"` -> 58 results, including the exact
    product. The route now does this automatically and tells you when it
    had to broaden the search.
- **Barcode path**: generated a real EAN-13 barcode image encoding
  `0050000993345`, ran it through `/food/search-photo`, and it correctly
  decoded the barcode and returned the exact product (name, real photo,
  full per-serving nutrition) via the exact-match endpoint.
- **OCR path**: rendered a synthetic label image reading "Starbucks /
  Caramel Macchiato" (no barcode), ran it through the same endpoint, and it
  OCR'd the text, cleaned it, and found the same product via search.
- **Blank/unreadable photo**: returns a clear "couldn't find a barcode or
  readable text" message rather than hanging or erroring opaquely.
- Reconfirmed the original hard-won lesson still holds with the new photo
  route: `POST /food/search-photo` returns in 0.00s regardless of how long
  barcode decode, OCR, or the Open Food Facts call take -- all of that
  happens on the background worker thread, never in the request handler.

## Hard-won lessons, still enforced

- No request handler waits on a live external call, a local image-decode
  call, or OCR -- all three now happen on the background worker, for both
  the text-search and photo-search job kinds.
- Per-item retry/cooldown, per-attempt try/except -- unchanged from the
  original build, now shared by both job kinds via one worker loop keyed
  on `job["kind"]`.
- Real User-Agent on every Open Food Facts call.
- Per-serving vs per-100g handled explicitly, sodium x1000 -- unchanged.

## US units

Nutrition Facts figures stay in grams/mg -- that's not a metric-vs-US
choice, real US FDA labels are gram-based too. Where US vs metric actually
applies is body weight and height, which live in the not-yet-built
Weigh-In Log / Dashboard; those will use lbs and ft/in when built.

## Meal photo logging (Bunny.net + Claude vision)

New: `POST /log/photo` -- snap a photo of a home-cooked or unpackaged meal
(distinct from the barcode/OCR product lookup, which is for packaged
products already in Open Food Facts). Pipeline, all on the background
worker per the usual rule:

1. Upload the photo to Bunny.net storage (`_upload_to_bunny`) -- one
   retry on 5xx, same pattern as the Open Food Facts calls.
2. Ask Claude (`claude-haiku-4-5-20251001`, cheap/fast is plenty for a
   rough single-number guess) for a calorie estimate + short description,
   via the Messages API with an image content block. Response is
   requested as bare JSON; parsing strips markdown fences if present and
   falls back to pulling the first number out of the text if JSON
   parsing fails outright, so a formatting slip doesn't lose the estimate.
3. The `FoodLogEntry` is created directly (no candidate list to confirm,
   unlike food search) with `ai_calories` set.
4. `POST /log/<id>/adjust` lets you correct the number afterward --
   that's the "manual adjustment field right next to it" from the
   original spec, since vision can't judge portion size or hidden
   oil/butter. Once adjusted, `manual_calories` always wins over the AI
   guess, and the "AI Estimate" badge disappears from the timeline.

Tested with mocked Bunny/Anthropic responses (no real credentials
available in this environment): confirmed the request handler still
returns in 0.00s regardless of how long those calls take, confirmed
markdown-fenced JSON parses correctly, confirmed a missing config
(`BUNNY_STORAGE_ZONE`/`ANTHROPIC_API_KEY` unset) fails with a clear
message instead of crashing, and confirmed manual adjustment correctly
overrides the AI value everywhere it's read.

Needs three new environment variables (`BUNNY_STORAGE_ZONE`,
`BUNNY_STORAGE_API_KEY`, `BUNNY_PULL_ZONE_HOST`) plus `ANTHROPIC_API_KEY`
-- reuses the same Anthropic API key already set up for WannaPeek, no
second key needed.

## Deployment note: two new system packages

`pyzbar` and `pytesseract` are Python wrappers around C libraries
(`libzbar0`, `tesseract-ocr`) that aren't installed by DigitalOcean's
Python buildpack by default. I added an `Aptfile` (verified this is a real,
documented DO App Platform mechanism -- the `heroku-buildpack-apt`
buildpack installs it during the build step) listing both packages. No
other action needed as long as the `Aptfile` deploys alongside the app.

The `pyzbar` import is wrapped in a try/except in `app.py` -- if
`libzbar0` isn't reachable at runtime for any reason, barcode decoding
just disables itself with a log warning rather than crashing the whole
app on startup.

**Full fix, done properly this time**: rather than patching one missing
library at a time as errors surfaced, ran `ldd` against the actual
compiled `tesseract` and `libzbar.so` binaries to get their complete
real dependency lists, then added every non-trivial one to the Aptfile
in a single pass:

- `tesseract-ocr-eng` -- the actual language data (`eng.traineddata`).
  The base `tesseract-ocr` package is just the engine; without this it
  runs but has nothing to read text with (`Error opening data file
  .../eng.traineddata`, the second error hit live).
- `libarchive13` -- tesseract's archive-format dependency (first error
  hit live).
- `libdbus-1-3`, `libv4l-0`, `libx11-6`, `libjpeg-turbo8`, `libsystemd0`,
  `libxcb1`, `libcap2`, `libgcrypt20`, `libxau6`, `libxdmcp6`,
  `libgpg-error0`, `libbsd0`, `libmd0` -- zbar's full runtime dependency
  chain (it's compiled with optional camera/X11/D-Bus support baked in,
  even though this app only ever decodes still images). Confirmed via
  `ldd` that these are genuinely linked, not guessed.

Root cause underlying all of this: DigitalOcean's Aptfile mechanism
(`heroku-buildpack-apt`) installs exactly the packages you list and
nothing those packages themselves depend on -- unlike a normal
`apt install`, it does not resolve dependencies. Every package in the
tree above had to be named explicitly.

## Deployment note: psycopg2-binary vs. Python 3.14

Hit this live during deployment: DigitalOcean's buildpack picked Python
3.14 for the app, and `psycopg2-binary==2.9.9` fails to import under it
with `undefined symbol: _PyInterpreterState_Get` -- a known, documented
upstream incompatibility between psycopg2's compiled C extension and
newer CPython internals (confirmed via psycopg2's own GitHub issues,
first reported at Python 3.13 and still unresolved at 3.14). Confirmed
locally that the exact same `psycopg2-binary==2.9.9` imports cleanly
under Python 3.12.3, so added `runtime.txt` pinning the buildpack to
`python-3.12.8` -- a mature version with known-good psycopg2 wheels.
No code changes needed, just the version pin.

## Running it locally

```bash
pip install -r requirements.txt --break-system-packages
python3 app.py
```

Needs `libzbar0` and `tesseract-ocr` installed locally too (already present
in this build environment) for the photo-search path; everything else
degrades gracefully without them except that specific feature.

Defaults to SQLite (`weighttrack.db`) if `DATABASE_URL` isn't set. Visit
`http://localhost:5000/food`.

## Not yet started

- Weigh-in log (7-day rolling average chart, streaks, milestones, notes
  field) -- also where the Weight/Streak stat cards get real data instead
  of "Coming Soon"
- Dashboard (Mifflin-St Jeor calorie target vs. intake/exercise) -- also
  where "Calories Today" gets a "/ target" comparison
- Meal photo logging (AI calorie estimate + manual adjustment) -- distinct
  from the barcode/OCR product lookup built this round; this one's for
  home-cooked/unpackaged meals
- Vacation/travel mode
- "On This Day" (Wikipedia) + Patriots RSS feed
- USDA FoodData Central fallback (needs a free API key, not yet obtained)
- GitHub repo + DigitalOcean App Platform deployment
