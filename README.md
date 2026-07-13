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

**Fourth issue, hit live after all of the above**: `tesseract-ocr-eng`
installed without any build error, but tesseract still couldn't find
`eng.traineddata` at its compiled-in default path
(`/usr/share/tesseract-ocr/4.00/tessdata`). Root cause: the Aptfile
buildpack extracts packages into an internal layer rather than merging
them into the filesystem paths an app expects. Shared libraries still
resolve because the dynamic linker's search path gets pointed at that
layer -- but a hardcoded data path like this doesn't get the same
treatment, and the buildpack doesn't document what its actual layer path
is, so guessing it would just be another one-shot patch with the same
risk of being wrong.

Fixed properly instead: `_locate_and_configure_tessdata()` in `app.py`
searches a handful of plausible locations at startup (including
`/layers/**/tessdata/eng.traineddata` and a few other buildpack-style
paths) and points `TESSDATA_PREFIX` at whichever one actually has the
file -- verified locally that this correctly adapts to wherever the
file really is (found it at a `tesseract-ocr/5/...` path here, different
from the `4.00` path DO's error showed, proving it's not hardcoded to
one guess). If the search comes up empty on some future environment,
`OCR_AVAILABLE` is `False` and OCR-based photo search degrades to a
clear "couldn't find a barcode or readable text" message rather than a
raw tesseract error reaching the user -- tested this exact scenario
directly (forced `OCR_AVAILABLE = False`) and confirmed the whole
photo-search flow still completes cleanly instead of erroring.

**Fifth issue: OCR accuracy on a real product photo.** Once the
infrastructure was actually working, the OCR itself was reading garbage
off a real Starbucks product photo. Root-caused with the real uploaded
file, not a synthetic test:

- The old `_best_guess_from_ocr` picked the single *longest* OCR'd line
  as its one guess. On the real photo, the longest line was garbled
  serving-size text ("CAAT 55 SERVINGS"), while the actual product name
  ("Caramel Macchiato Almondmilk & Oatmilk Creamer") was split across
  four separate *shorter* lines and never got picked. Fixed by combining
  every plausible line into one query instead of picking just one, then
  letting `_progressive_search`'s existing trailing-word cascade narrow
  it down.
- Default OCR (no preprocessing) completely missed lower-contrast label
  text (white text on an orange band). Grayscale + autocontrast before
  OCR recovered it -- confirmed directly against the real file: without
  preprocessing, "Caramel Macchiato" never appeared in the OCR output at
  all; with it, both "Caramel" and "Macchiato" read cleanly.
- `_progressive_search` had a real bug: a transient error on its first
  (longest) candidate query aborted the entire search rather than
  falling back to shorter candidates. Fixed so it only reports an error
  if every candidate in the cascade failed, and distinguishes that from
  a genuine "no matches" (which requires at least one candidate to have
  gotten a clean response) -- otherwise a real search failure could have
  been misreported as "this product isn't in the database."

End-to-end confirmed against the actual uploaded photo after all of the
above: correctly identifies the exact product, first result, matching
barcode, real photo included.

**Sixth issue: I broke it myself with the "resilience" fix.** Bumping
`OFF_RETRY_COUNT` and making `_progressive_search` keep trying shorter
candidates after a failure (issue five, above) had a real side effect I
didn't account for: for a long OCR-derived query (up to 12 words), the
cascade could generate 10+ candidate queries, each retried up to 3
times -- worst case, dozens of HTTP attempts, easily exceeding the
client's ~20s poll timeout ("This is taking longer than expected").
Hit live.

Fixed by decoupling two things that were multiplying against each
other: only the first (longest, most-likely-correct) candidate gets
retried on a transient error; fallback candidates get one fast attempt
each and move on immediately, since trying the next candidate already
functions as a retry. Also capped the cascade at 4 candidates total
(using bigger step sizes) instead of one-word-at-a-time down to the
floor. Measured worst case directly (every request failing, a 12-word
query): **3.0 seconds, 6 total HTTP calls** -- down from a worst case
that could previously run past a minute. Bumped the client-side poll
timeout to ~36s as an extra safety margin on top of that. Re-confirmed
the real photo still finds the correct product afterward (8.75s,
correct brand/product, real barcode).

**Seventh issue: line-based garbage filtering wasn't enough.** A
different real photo produced OCR output like `"Bais ProLcoueL ry
NON-DAIRY ALMONDMILK & OATMILK CREAMER ETT PO reo MAT aE na"` -- real
text sandwiched between garbage on *both* sides. The line-based
filtering from issue five only trimmed candidates from the end of the
string (via `_progressive_search`'s trailing-word cascade), so every
candidate still dragged the leading garbage along and never isolated
the real text.

Root cause: guessing which OCR lines are "real" from surface heuristics
(length, alphabetic content) doesn't work when noise can appear
anywhere. Fixed properly by using Tesseract's own per-word confidence
score instead of guessing -- switched from `image_to_string` to
`image_to_data`, which returns a confidence value (0-100) per detected
word, and drop anything under 60. Confirmed directly against the real
photo that this actually separates signal from noise: real label words
scored 80-96 (CARAMEL 96, MACCHIATO 90, ALMONDMILK 91, OATMILK 89,
CREAMER 96, NON-DAIRY 91), while stray punctuation and misread
fragments scored 0-30. This works regardless of *where* in the OCR
output the garbage falls, unlike the previous end-trimming approach.

Re-confirmed end-to-end against the real photo after this change: 4.36
seconds, exact correct product and barcode as the first result.

**Eighth issue: Open Food Facts had two entries for the same real
product, and the app confirmed the wrong one.** A different photo
(Starbucks Hazelnut Latte creamer) matched a real barcode with a real
photo but zero nutrition data. Confirmed live this wasn't a bug in how
this app reads OFF's response -- that exact barcode genuinely has no
nutriments on Open Food Facts. But a *second*, more complete entry for
the same product exists under a different barcode, and the app had no
way to know to prefer it.

Fixed two ways: (1) `_rank_products()` sorts every result list so
entries with both a photo and real nutrition data come first, instead
of confirming whichever OFF happened to return first. (2) When a
scanned barcode's exact entry is missing nutrition data, the app now
runs a supplementary search to look for a more complete duplicate --
searching the *full* product name doesn't work here (confirmed live it
just re-matches the same sparse entry by its own exact name), so it
uses a shorter brand + first-few-words query instead, which confirmed
live surfaces both entries. Re-tested against the real barcode: the
complete duplicate (30 cal) now ranks first, with a note explaining
what happened, instead of silently handing over the sparse one.

Also bumped both search-result and library-card thumbnail sizes
(48px -> 96px photos, library cards ~45% wider) since they were too
small to evaluate a result at a glance.

One thing worth being upfront about rather than promising a fix for:
Open Food Facts is entirely crowdsourced, so there's no way to filter
for "official stock photos only" -- every image on it was uploaded by
some contributor, brand or random shopper, with no field distinguishing
one from the other. Popular products tend to have decent photos since
they get more contributions; less common variants sometimes only have
someone's phone snapshot. The ranking fix surfaces the *better*
available entry when duplicates exist, but can't guarantee photo
quality on entries where only one exists.

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

## Decision: OCR removed entirely, barcode-only photo search

After the chain of issues above (garbled OCR, hyphenated words silently
breaking Open Food Facts' search, sparse duplicate entries), cut OCR out
of the photo-search path entirely rather than continuing to patch it.
`/food/search-photo` now only tries barcode decoding -- if no barcode is
found or decoded, it says so plainly and points to the text search
instead of falling back to label-reading. Also removed `tesseract-ocr`,
`tesseract-ocr-eng`, and `libarchive13` from the `Aptfile` and
`pytesseract` from `requirements.txt`, since nothing calls it anymore.
Barcode decoding (`libzbar0` + its dependency chain) is untouched and
still works exactly as before.

Manual text search (typing a product name) is unaffected -- it never
used OCR, and still has the query-cleanup and progressive-search
fallback logic from earlier fixes. The hyphenated-word search bug found
during this session (e.g. "Non-Dairy" or "Lactose-Free" in a query can
zero out Open Food Facts results even when spelled correctly) is still
present for anything typed manually with a hyphen in it -- not fixed,
just no longer reachable via the OCR path that made it show up
constantly.

## Weigh-In Log, Dashboard, Vacation Mode -- now wired up

All three nav tabs that were placeholders are real pages now.

**Weigh-In Log** (`/weigh-in`): log weight in lbs + optional notes,
7-day rolling average (not raw daily dots, per the original spec --
those are too noisy to be useful), a hand-rolled SVG line chart (no
charting library needed for one line), streak counter, and simple
milestone callouts (first entry, weight change since your first entry,
streak multiples of 7).

Streak logic is vacation-aware: tested directly with a 10-day span
that had a 2-day gap covered by a vacation period, and confirmed the
streak correctly comes out to 10 (vacation days count as grace days
toward the number, not just "don't break it silently") -- also tested
that an *uncovered* gap correctly breaks the streak back down to 1,
so the vacation exemption doesn't accidentally swallow real misses.

**Dashboard** (`/dashboard`): daily calorie target via the Mifflin-St
Jeor equation (needs height, age, biological sex from your profile, and
your latest weigh-in for current weight), today's consumed calories,
exercise logged (simple activity + calories-burned quick-add), and
remaining = target - consumed + burned. Sanity-checked the formula
output against plausible real-world ranges for both sexes before
shipping, not just checking it ran without erroring.

**Vacation Mode** (`/vacation`): add a trip (label + date range), see
what's currently away / upcoming / past. Currently-active trips are
called out separately with a note that the streak is protected.

Also fixed a real pre-existing bug while wiring the Weight/Streak/
Calories-Today stat cards to actual data: "Calories Today" was computing
totals via `.scaled("calories")` directly instead of the `.calories`
property, which meant photo-logged meals (no linked FoodItem) always
contributed 0 regardless of their real AI-estimated or manually-adjusted
calories. Fixed and confirmed with a real photo-logged entry that it now
counts correctly on both the Food Library and Dashboard pages.

## Ninth issue: barcode decoding was never actually working on DigitalOcean

The "Unable to find zbar shared library" warning that seemed fixed by
adding dependency packages earlier was never actually fixed -- that fix
targeted the wrong failure mode. Root-caused properly this time by
reading pyzbar's actual source: on Linux, it finds the zbar library with
exactly one mechanism, `ctypes.util.find_library('zbar')`, which
searches the system's `ldconfig` cache -- not the filesystem directly.
DigitalOcean's Aptfile buildpack installs `libzbar0` into an internal
layer without ever running `ldconfig` to register it there, so
`find_library` returns `None` even though the file is genuinely present
on disk. This is an earlier, different failure stage than the tesseract
dependency issue from earlier in the night: the library is never even
*located*, let alone loaded, so no amount of adding libzbar0's own
dependencies to the Aptfile could ever have fixed it. That's why the
13-package fix didn't help -- confirmed live it was still failing with
the identical warning afterward.

Confirmed this diagnosis two ways before trusting it: (1) reproduced the
exact failure locally by monkey-patching `find_library('zbar')` to
return `None`, matching the live warning exactly, and (2) confirmed
`find_library` succeeds without any patching in this local environment,
explaining why the bug never showed up in testing here despite being
present on DigitalOcean the whole time.

Fixed by patching `find_library` to fall back to a direct filesystem
search (same technique as the tessdata fix) specifically for "zbar",
before pyzbar's import-time load runs -- restored immediately afterward
so it doesn't affect any other library lookup in the app. Tested
end-to-end under the exact simulated failure condition: barcode
decoding, and a full photo search against a real barcode file, both
work correctly even when `find_library` is broken exactly like
DigitalOcean's environment.

## AI food-logging agent, backdated weigh-ins, pantry/scanning removed

Three related changes:

**Backdated weigh-ins**: the weigh-in form now has an optional date field.
Leave it blank for today, or pick an earlier date to backfill a past
entry. No special "recalibrate" logic was needed -- milestones, streaks,
and the "first entry" reference already sort by `logged_at` at query
time rather than insertion order, so backfilling an earlier date
automatically becomes the new day one. Tested directly: backdated an
entry 10 days earlier than the existing one and confirmed it's treated
as the earliest without any extra code.

**Pantry ("On the Shelf") and barcode/text scanning removed** from the
Food Library page entirely, per direct request -- not hidden, removed
from the template. `/food/search`, `/food/search-photo`, and the
underlying barcode-decode machinery are all still there in `app.py` (no
reason to delete working, tested code), just no longer linked to from
the UI.

**New: AI food-logging agent.** Pick a meal type from a dropdown, type
what you had in plain language (e.g. "two eggs, wheat toast with peanut
butter, black coffee"), and Claude (`claude-sonnet-5` -- stronger
reasoning than the Haiku model used for the quick photo-calorie guess,
since this has to split one sentence into several distinct items with
individual macro estimates) breaks it into separate FoodLogEntry rows,
each with its own calorie/protein/carb/fat estimate, logged directly --
no search, no confirmation step. Meal type comes from the dropdown, not
from Claude's guess, which removes a whole category of ambiguity.

Tested end-to-end with the exact example from the request -- "two
pieces of wheat toast with jif chunky peanut butter and 28 oz of coffee
with 3 tbsp of starbucks non-dairy creamer" -- confirmed it correctly
splits into 4 distinct entries (toast, peanut butter, coffee, creamer),
each with its own macros, all tagged to the selected meal type, showing
up correctly on the timeline with a real total.

**"Claude will learn and proactively offer up the meals in the past"**:
implemented as the app's own history rather than a separate ML system --
entries logged together in one agent submission share a `batch_id`.
`GET /agent/recent-meals?meal_type=X` returns the most recent distinct
batches for that meal type, shown as "log again" chips above the input.
Tapping one clones the batch's items as new entries logged right now --
no AI call needed for a repeat, just copying known-good data. Tested
directly: logged a meal, confirmed it appeared as a suggestion for that
meal type, and confirmed "Log Again" created a fresh set of entries.

## Tenth issue: new columns never reached the live database

Hit immediately after the last deploy: `psycopg2.errors.UndefinedColumn:
column food_log_entries.ai_protein_g does not exist`. Root cause is a
predictable, well-known Flask-SQLAlchemy behavior I should have caught
before shipping: `db.create_all()` only creates tables that don't exist
yet -- it never alters an *existing* table to add new columns.
`food_log_entries` already existed in production from the meal-photo-
logging deploy earlier tonight, so when `ai_protein_g`, `ai_carbs_g`,
`ai_fat_g`, and `batch_id` were added to the model for the food agent,
`create_all()` silently did nothing for that table, and every query
touching it threw a real SQL error in production.

Reproduced this exactly before fixing it: built a SQLite table matching
the live production schema (missing those four columns), ran the
current app against it, confirmed it fails the same way. Fixed with
`_ensure_schema_up_to_date()` -- a minimal, dependency-free migration
step (no Alembic in this project, appropriate for a personal app's
scale) that runs at startup, compares each model's declared columns
against what the live table actually has via SQLAlchemy's inspector,
and issues `ALTER TABLE ... ADD COLUMN` for anything missing.

Tested three ways: (1) against the reproduced old-schema database --
confirmed it adds exactly the four missing columns and the page loads
cleanly afterward, (2) against a completely fresh empty database --
confirmed it's a clean no-op when `create_all()` already created
everything correctly, (3) running it twice in a row against an
already-synced database -- confirmed it doesn't error or duplicate
columns on a second run.

## Floating chat is now general-purpose, assistant can adjust/delete entries

Two changes:

**Floating chat button** — dropdown removed entirely. It's a plain text
box now: ask a question, describe a meal (meal type gets inferred from
context or time of day), or correct something already logged. The
inline "Tell The Assistant" panel on the Food Library page keeps its
dropdown, since that's still the faster path when you know exactly
which meal you're logging.

**The assistant can now adjust and delete, not just create.** Every
call includes the 20 most recently logged entries (id, name, meal,
calories) as context, so when you say "actually the toast was 3
slices" or "delete the coffee I just logged," Claude can identify which
entry you mean and either update it or remove it -- same background job
as everything else, same one-message interface either way.

Tested five scenarios end-to-end before shipping: meal type correctly
inferred when not pre-selected (FAB), an adjustment correctly updates
the entry and shows the new number on the timeline, a deletion
correctly removes the entry, a pure question creates nothing and just
answers, and the inline dropdown still correctly overrides Claude's own
meal-type guess when it's provided. Also caught and fixed a real bug
during testing -- the new "recent entries" lookup was running outside
the Flask app context in the background worker thread, which would
have crashed every single request with "Working outside of application
context" if it had shipped as first written.

## Photo picker (Plex-style) + automatic photo memory

Every timeline entry now has a small camera icon that opens a
Plex-poster-picker-style modal: paste an image URL, it's added to that
entry's thumbnail row immediately, and you can add several without
closing the modal -- they render side by side, scaled to fit next to
the food line. No fetching or validating the URL server-side (that
would be a network call inside a request handler, which this app
avoids everywhere), so a bad URL just fails to load client-side rather
than blocking anything.

**Automatic reuse**: attaching a photo also saves it to a small
name-keyed memory (`FoodPhotoMemory`), independent of any single log
entry. Every future entry with a matching food name gets that photo
attached automatically -- no need to re-paste the same URL every time
you log the same thing. Matching is on the food name with any quantity
suffix stripped ("Wheat Toast (2 slices)" and "Wheat Toast (3 slices)"
match the same memory), so it works across different serving sizes.

Tested directly: attached a photo to a "Wheat Toast (2 slices)" entry,
then logged "Wheat Toast (3 slices)" separately and confirmed the photo
carried over automatically despite the different quantity; deleted the
original entry and confirmed the memory survived and a third entry
still got the photo; attached a second, different photo to the same
name and confirmed both appear in the correct order; re-added a
duplicate URL and confirmed the memory doesn't store it twice.

## Mobile/tablet hero dashboard, goal weight, program start date

**Device detection**: used CSS media queries on actual viewport width
rather than user-agent sniffing -- more robust (works correctly through
window resizing, rotation, foldables, and any future device with zero
code changes, unlike matching against a device's self-reported identity
string). Below 1024px width, a big-number hero dashboard appears at the
top of the Food Library page; above it, the existing layout is
unchanged.

**The hero shows**: pounds lost (green, starting weight minus current,
floored at 0), current weight (red), goal weight (gold, or a "Set" link
to the Dashboard if none is set yet), and a secondary row of streak,
7-day average, lbs to goal, and today's calories. "Day N" badge at top
counts from a program start date, defaulting to June 28, 2026 as
requested -- stored on the profile (not hardcoded elsewhere) and
editable later from the Dashboard.

Added `goal_weight_lbs` and `program_start_date` to `UserProfile` (both
new fields on the Dashboard's profile form) to support this. Tested the
actual math end-to-end with real weigh-in data (190 -> 175 lbs, goal
160, start date June 28): confirmed 15.0 lbs lost, 15.0 lbs to goal, and
14 days since start all compute and render correctly on the real page,
plus confirmed the empty-state (no weigh-ins or goal set yet) renders
without crashing.

## Exercise logging now AI-estimated, personalized to your profile

The manual "Cal burned" number field is gone. Describe the activity
("half a mile walk") and Claude estimates the calories using your
actual weight (from your latest weigh-in), age, biological sex, and
activity level from the Dashboard profile above it -- weight in
particular is a real, physiologically meaningful input to exercise
calorie burn (a heavier person burns more for the same walk), not
window dressing. Same background-job pattern as every other AI feature
in the app -- the request handler returns instantly regardless of how
long the Claude call takes.

Confirmed two things directly before shipping: (1) captured the actual
prompt sent to Claude and verified the real profile data (weight, age,
sex) is genuinely present in it, not just claimed, and (2) confirmed it
still works with a sensible generic fallback when no profile or
weigh-in data exists yet, rather than failing outright.

## Hero dashboard: gradient tile with color-coded chips

Refined per feedback into three rounds: first a dark surface with a
subtle glow, then a full radiant gradient tile matching the reference's
countdown bar exactly, then back to keeping the individual green/red/
gold color-coding rather than unifying to dark numbers -- resolved by
giving each stat its own dark chip on top of the gradient, so the
colored numbers stay legible regardless of which part of the
green-to-lavender-to-coral gradient they land on (green text directly
on the green part of the gradient would have been unreadable otherwise).
Font weight dropped from 800 to 600 to match the reference's lighter
number style. Secondary stats (streak, 7-day average, etc.) stay on the
plain dark surface below, so the radiant moment doesn't get diluted
across seven numbers.

## Chip resize + splash screen on first load

**Chips shrunk significantly**: padding cut from 14px to 6px, font size
from clamp(22-30px) down to clamp(15-20px) -- much more compact, per
direct feedback from a live screenshot showing them too large.

**New: splash screen on first load (mobile/tablet only).** Loading the
app fresh now shows a full-screen glanceable view of the core stats
(day counter, lbs lost as the big hero number, current and goal below
it) -- tap anywhere to dismiss into the real app underneath. Remembered
for the rest of the browser session via `sessionStorage` (not
`localStorage` -- a fresh session should see it again; this is a
"welcome back" moment, not a permanent one-time thing).

Avoided a flash-of-splash on repeat page loads within the same session
by hiding it synchronously: a tiny inline script sits as the *first
child* inside the splash div itself and checks `sessionStorage`
immediately as the parser reaches it, before the rest of the div's
content is even parsed -- not `document.write` (a legacy pattern with
real gotchas if ever called post-load), just `document.currentScript.
parentElement.style.display = "none"`, which is safe and synchronous
here since it always runs during initial page parse.

## Timestamp correction, hero dashboard on desktop, persistent floating chat

**Assistant can now correct WHEN something was logged.** Real gap: the
adjustment schema only covered nutrition fields, so asking it to fix a
timestamp ("that was actually at 7am") had nothing to act on and it
correctly said it couldn't -- not a bug in the sense of broken code, a
missing capability. Added `date`/`time` fields to the adjustment
schema; the assistant now updates `logged_at` directly, keeping
whichever of date/time wasn't mentioned unchanged. Tested three ways:
time-only correction, combined date+time correction, and a malformed
time value from the AI (confirmed it skips just the time change rather
than crashing the whole request).

**Hero dashboard now shows on desktop too.** It was deliberately scoped
to mobile/tablet only when first built, matching the original "for
mobile" request -- reasonable at the time, but there was no real reason
to hide a good piece of UI from desktop once it existed. Unwrapped it
from the `max-width: 1024px` media query entirely; the splash screen
stays mobile-only on purpose (a full-screen lock-screen-style gate
makes sense on a phone, not really as a desktop browsing pattern).

**Floating chat is a real conversation now**, not a fire-and-reload.
Previously every message triggered a full page reload after ~1.4s,
which wiped the open modal -- exactly the complaint. Rebuilt as a
persistent thread: messages append as bubbles, the modal stays open
across multiple exchanges, and the underlying page only reloads once,
when the modal is *closed*, and only if something was actually changed
during the conversation (tracked via whether any job returned created/
adjusted/deleted entries) -- a pure question changes nothing, so
closing after one doesn't reload at all. Confirmed the response shape
each check depends on directly: a logging action returns non-empty
entries/adjusted/deleted, a pure question returns all three empty.

## Location fix, stamp cleanup, tighter gradient tile

**Location now includes full state and country**, not just an
abbreviated region code. One honest caveat: the "city" portion may
still show "Central Coast" for that exact spot, if that's genuinely the
most specific place name BigDataCloud's reverse-geocode API has for
those GPS coordinates (a data-source limitation, not something more
code can fix) -- but state and country are real, added information now
("Central Coast, California, United States" instead of "Central Coast,
CA").

**Stamp box cleaned up**: combined date and time onto one line (they
were three separate equal-weight labeled rows before, which read as
cluttered for what's really one piece of information plus a secondary
detail). Date/time font roughly doubled (11px -> 22px) as the primary
line; location is a smaller secondary line below it, giving it an
actual hierarchy instead of three flat rows. Added a narrow-screen
adjustment so the bigger text doesn't overflow small phones.

**Gradient hero tile tightened**: it was spanning the full page width
with the chip row centered inside, leaving wide unused gradient margins
on both sides. Constrained the tile itself to hug the chip row's width
instead of stretching edge to edge.

## Assistant now sees profile/weigh-in data, and the chat has real memory

Two real bugs compounding in that screenshot, both confirmed and fixed
directly against the exact scenario shown.

**Missing data visibility.** The assistant only ever saw the 20 most
recent food log entries -- nothing from the profile, weigh-in log, or
the app's own calorie-target calculation. So "recalculate my daily
calorie intake" had genuinely nothing to work with, even though that
data exists elsewhere in the app. Now every call includes current
weight, age, sex, height, activity level, goal weight, and -- important
-- the app's own already-computed calorie target (same Mifflin-St Jeor
number the Dashboard shows), so the assistant references that instead
of deriving a second, possibly-different number on its own. Confirmed
by capturing the actual request sent to Claude and verifying the real
profile data is in it, not just claimed.

**No conversation memory.** Separately, and probably the more visible
half of the breakdown in that screenshot: each message in the floating
chat was being sent to Claude as a fresh, isolated request with zero
awareness of what was just discussed -- the UI *looked* like a
persistent thread, but the backend was answering every turn blind. That
explains the "Got it, thanks! That's just general info about the site
though" reply to "most of it is in the weigh-in log or dashboard
sections" -- Claude had no idea what "it" referred to. Fixed by
threading real conversation history through: the frontend now tracks
each exchange and sends it along, the backend replays it as actual
message history (via the Messages API's `system` + `messages`
structure) rather than cramming everything into one message every
time. Confirmed directly: captured the messages array sent to Claude on
a simulated turn 2 and verified the full prior exchange is there, not
just the new message in isolation.

## "Couldn't parse" fixed for real via tool-use, plus microphone dictation

**The parsing failure from that screenshot is now structurally
impossible**, not just patched. Root cause: I'd been asking Claude to
respond with plain-text JSON and hoping it stayed disciplined about the
format. Confirmed live that over a longer, more natural conversation
the model can drift into wrapping the JSON with conversational prose
("Sure, let me help with that. {...}"), which broke the old regex-based
extraction outright. Rebuilt the whole response contract on Claude's
actual tool-use feature: a forced `log_and_reply` tool with a real JSON
schema, via `tool_choice: {"type": "tool", "name": "log_and_reply"}`.
This makes structure a guarantee from the API itself, not an
instruction the model has to keep remembering to follow -- extraction
is now just reading an already-parsed `input` dict off a `tool_use`
content block, no string parsing at all. Tested by deliberately
reconstructing the exact failure shape (a leading text block *plus* the
tool call, mimicking conversational drift) and confirmed extraction
still works cleanly; also re-confirmed full food logging still works
end-to-end against the new schema.

**Microphone dictation** added to both the inline "Tell The Assistant"
panel and the floating chat, using the browser's native Web Speech API
-- no external service, no added cost, works offline of any server
call. Degrades gracefully (button visibly disables with an explanatory
tooltip) in browsers that don't support it rather than erroring.

## Real per-day chart with hover data, plus BMI everywhere

**Chart rebuilt entirely.** Replaced the 7-day rolling-average SVG with
a genuine per-day chart (Chart.js, loaded via CDN only on this page --
the rest of the app stays dependency-free) plotting one point per
actual logged weigh-in, not a smoothed average. Hovering a point shows
that day's calories consumed and burned, computed fresh from
`FoodLogEntry`/`ExerciseEntry` per day. Tested the exact reported
scenario directly: added a new weigh-in (349.6) and confirmed it
appears in the chart data on the very next page load, and confirmed
the hover payload for a day with real food/exercise logged shows the
correct consumed/burned numbers, not placeholders.

**BMI added everywhere**, per "add that metric to all major tiles":
Food Library's stat row, the mobile hero gradient tile, the splash
screen, the Weigh-In Log's stat row, and the Dashboard's stat row --
five locations, all pulling from one shared `_calculate_bmi()`/
`_bmi_color()` pair so they can't drift out of sync with each other.
Color-coded green (under 25, "good"), yellow (25-29.9, "moderately
over"), red (30+, "severely over") -- verified the formula against two
known real-world reference BMI values and confirmed all three tier
boundaries land exactly where expected (24.9 green, 25.0 yellow, 29.9
yellow, 30.0 red) before wiring it into any template. Falls back to a
"Set Height" prompt rather than a blank or wrong number when the
profile's height isn't filled in yet.

## Exercise calorie accuracy fixed, and dates now actually parsed

**The 104-calorie estimate for a 1-mile walk was genuinely wrong**,
confirmed against the standard MET formula by hand first: for this
user's actual weight (~350 lbs from BMI 50.2 at 70in), a casual-pace
1-mile walk should burn roughly 185 calories, not 104 -- a real,
significant undershoot, not just noise. Root cause: Claude was being
asked to produce the final calorie number in one step, meaning it had
to silently do "MET x weight_kg x duration_hours" as mental arithmetic
on non-round real numbers, which isn't reliable even when the method is
right. Fixed by splitting the task in two: Claude now only identifies
an appropriate MET value and duration for the described activity
(language understanding, which it's good at), and the app does the
actual multiplication itself in Python against the real logged weight
-- deterministic, not another AI guess. Re-ran the exact scenario after
the fix: 185 calories, matching the hand-calculated expected value
exactly.

**Dates are now actually parsed for exercise too.** Separate bug caught
in the same message: "yesterday I walked 1 mile" was always being
logged with *right now's* timestamp regardless of what was said --
exercise logging never had the date-inference the food agent already
had. Fixed the same way: Claude returns an inferred date, the entry
gets stamped with it. Confirmed directly that a "yesterday" entry lands
on yesterday's date, and confirmed this flows through correctly to the
day-by-day weight chart's hover data -- calories burned now show up on
the actual day they happened, not lumped into today.

## Exercise History: the real cause of "jacked up" metrics

Reproduced this precisely before building anything: "Today's Exercise"
only ever queried *today's* entries, so a "yesterday I walked a mile"
entry existed in the database (confirmed working correctly per the
last fix) but was genuinely invisible anywhere in the UI. Retrying it
several times, reasonably assuming it hadn't worked, silently stacked
up duplicate entries on that same day with no way to see or catch it.
Simulated the exact reported numbers (four ~185-calorie walk entries on
one day) and got 732 total calories burned -- matching the screenshot's
figure precisely, strong confirmation this was really what happened.

Added an **Exercise History** section to the Dashboard: every entry
from the last 30 days, grouped by day with a per-day total, each entry
individually deletable with the same remove button already used
elsewhere. Confirmed end-to-end: all duplicate entries are now visible,
the day total matches the real (inflated) figure, deleting one drops
both the day total and the weight chart's hover data correctly.

## Exercise entries fully in the assistant's reach, plus every AI feature shares one context

**The screenshot's exact failure**: asking the floating chat to adjust
an exercise entry's time got "I don't see an exercise entry logged" --
because the assistant's context only ever included food entries, never
exercise. Extended the tool schema with `exercise_items` (new exercise,
logged the same MET-based deterministic way as the dedicated form) and
an `entity` field on adjustments/deletions (since food and exercise ids
overlap and need disambiguating). Confirmed directly: asked it to move
an exercise entry to 6:15 PM yesterday, got a correctly-timestamped
update back.

**Then went further**: audited every AI-driven feature in the app and
found the other two -- the dedicated Log Exercise form and the meal-
photo calorie estimator -- had their own narrow, inconsistent slices of
app data (one saw only the latest weight, the other saw nothing at
all). Pulled all the context-gathering into one shared
`_gather_app_context()` -- profile, latest weight, the app's own
calorie-target calculation, recent food, recent exercise -- so every AI
feature sees the same real picture instead of maintaining its own
independent, driftable idea of what data exists. The exercise estimator
now also gets recent-exercise visibility specifically so it can flag a
likely duplicate before it's logged, and the meal-photo estimator now
sees recent food for estimate consistency. Confirmed by capturing the
actual prompts sent to Claude for both and verifying the real data is
present, not just claimed; also re-ran the existing food-agent test
suite after the refactor to confirm nothing regressed.

## Timezone bug fixed, navigation rebuilt around Dashboard / Log / Vacation

**The "carryover from yesterday" timeline was a real, provable bug, not
just a UX complaint.** Root cause: every "today" boundary in the app
was computed from raw UTC (`datetime.utcnow().date()`), with zero
timezone conversion. For anyone west of UTC (this user is in Pacific
time), anything logged after about 5pm local time gets a UTC timestamp
that's already rolled over to the next UTC calendar day -- so an entry
logged "last night" shows up in "today's" timeline the next morning,
and inflates "today's" calorie total right along with it. Proved the
mechanism by hand first (a 6:42pm Pacific entry lands on UTC's *next*
calendar date), then reproduced it directly against the app: the same
entry showed up in "today" with no timezone cookie set, and was
correctly excluded once one was.

Found this fix already partially built from earlier in the session --
the low-level timezone-conversion helpers existed, but two critical
links were missing: (1) the actual frontend piece that detects the
browser's IANA timezone and sets the cookie the server reads had never
been written, and (2) several route handlers that enqueue background
jobs never captured that cookie into the job dict, meaning the
background worker thread (which has no Flask request context at all)
would have crashed outright trying to read it. Both are now wired up:
`resolveTimezone()` in `main.js` sets the `wt_tz` cookie once per
session (reloading once so the very first page load isn't rendered
against stale UTC boundaries), and every job-enqueueing route now
threads `request.cookies.get("wt_tz")` into the job before handing it
to the worker. Also caught and fixed a dropped variable reference
(`chart_data` never actually being computed) left over from that same
partial refactor, and converted three remaining raw-UTC call sites
(backdating a weigh-in, vacation period bucketing, exercise history
day-grouping) that had been missed. Timeline and history timestamps now
also display in local time via new `local_time`/`local_date` Jinja
filters, instead of showing the raw UTC clock time mislabeled as local.

**Navigation rebuilt around function, not data type.** Per direct
feedback that mixing "enter a metric" with "see a metric" on the same
screens made the flow hard to follow, consolidated four pages into
three: **Dashboard** (every metric, chart, and history list -- entirely
read-only aside from cleanup actions like deleting a mistaken entry),
**Log** (every input field in the app -- food, exercise, weigh-in,
profile settings -- and nothing else), and **Vacation Mode** (unchanged).
Kept every underlying route and endpoint name alive (so nothing bookmarked
or linked internally breaks) -- `/weigh-in` now redirects to `/dashboard`
since its content lives there, and `/food` is still the URL for the Log
tab, just relabeled and rebuilt. Confirmed the actual separation
directly: checked that the Dashboard page contains zero `<form>` input
elements and the Log page contains zero metrics/timeline content, then
confirmed a real end-to-end flow (save a profile on Log -> BMI updates
correctly on Dashboard) actually works across the new page boundary.

## Post-restructuring audit: one real dropped feature found and fixed

Went through the whole app systematically rather than spot-checking:
every `url_for()` in every template verified against the actual route
map, every route cross-referenced against a JS caller, every element
ID and `data-*` attribute the JS listens for checked against where it
now actually lives after the page split, every empty-state link and
hint string checked for stale references to the old nav.

**Found one real gap**: the weigh-in entries list (with per-entry
delete) existed on the old standalone Weigh-In page, but when its
content got merged into the new Dashboard, only the streak/chart/
milestones came along -- the actual entries list never did. The
`/weigh-in/<id>/delete` route was still there and working, but nothing
in the UI could reach it anymore. Confirmed this concretely (logged two
weigh-ins, checked the delete control literally didn't exist on the
page) before fixing it -- restored the list under the chart, same
pattern as Exercise History (view + delete, no new-input fields, so it
belongs on Dashboard not Log).

Everything else checked out: the handful of "missing" element IDs
turned out to all be the already-known, already-guarded pantry/
barcode-scanning leftovers from earlier in the session (harmless,
intentionally inert, not new); "Today's Exercise" isn't a separate
section anymore but is still fully visible as the first, "Today"-
labeled group inside Exercise History rather than actually dropped;
every JS init function that touches elements now split across two
pages (the weigh-in form vs. its delete buttons, the profile/exercise
forms vs. their history views) was already written with `if (element)`
guards, so calling all of them unconditionally on every page -- which
the app already did before this restructuring -- continues to work
correctly with no changes needed there.

## Likely real cause of the timeline bug persisting: stale cached JS

Re-verified the timezone fix's backend logic first, from scratch,
before assuming anything about the browser: reproduced the exact
scenario again (a yesterday-evening Pacific entry) with the `wt_tz`
cookie set, and confirmed it's correctly excluded from "today." The
server-side fix holds.

That points at the browser never actually running the *new*
`resolveTimezone()` code that sets the cookie in the first place --
almost certainly stale caching of `main.js`. This file changed on
nearly every redeploy tonight, but nothing ever forced a browser to
fetch the new copy instead of serving whatever it cached from an
earlier visit; a shipped fix could sit there completely inert until a
manual hard refresh happened to clear it, with no signal anywhere that
this was the problem. Added real cache-busting: static asset URLs now
carry a `?v=<mtime>` query string computed fresh per request from each
file's actual last-modified time, so any future deploy is guaranteed to
invalidate the browser's cached copy automatically. Confirmed the
version string changes when the file's mtime changes, which is exactly
what happens on every real deploy.

If the timeline issue is still showing after this deploy, a hard
refresh (Cmd+Shift+R / Ctrl+Shift+R) once should clear out whatever's
currently cached; every deploy after this one won't need that.

## Dashboard: Weigh-In Entries and Exercise History in two columns

Per direct request, split the Dashboard's history section into two
columns side by side (Weigh-In Entries left, Exercise History right),
with Today's Timeline moved to a full-width section below both.
Collapses back to a single stacked column under 860px so it doesn't
get cramped on mobile/tablet -- the two-column layout is a desktop-
width thing, not something that should survive onto a phone screen.
Caught and fixed a duplicate closing `</section>` tag left over from
the restructuring during testing, then confirmed the actual rendered
order is correct (columns first, timeline after) and that delete/
clear-day actions still work correctly inside the new layout.

## Timezone default hardcoded, timeline now grouped by day with bold headers

**Stopped relying on the cookie as the thing standing between this app
and correct behavior.** The private-browsing screenshot was the key
evidence: a fresh session, no old cache, no old cookies -- and the bug
was still there. That rules out caching as the cause in that instance,
and means cookie-based detection has now failed twice in two different
ways. For a single-user personal app, defaulting to UTC every time
detection fails is actively harmful (it's never correct for this
user); defaulting to this specific user's actual, already-confirmed
timezone (Pacific, per the location stamp) is a far smaller risk.
Changed both `_user_timezone()` and `_job_tz()`'s fallback from UTC to
`America/Los_Angeles`. The cookie still refines this when it happens to
work; it's no longer load-bearing for correctness. Re-tested the exact
worst case directly -- zero cookie support at all -- and confirmed the
day-boundary bug is gone regardless.

**Timeline now grouped by day with a bold header per day**, directly
per request. Renamed to "Recent Timeline" (last 7 days) since it can
now show more than just today, each day getting its own visually
distinct heading and its own calorie total -- entries can no longer
blend together across days the way they did before, and if a day-
boundary issue ever happens again, it would show up immediately as an
entry sitting under the wrong header instead of hiding silently.
Confirmed the "Today" group's total matches the top-level "Cal Today"
stat exactly, and confirmed entries actually land under the correct day
header even with zero timezone cookie support.

## "Today" header still showing wrong entries: old data, not a new bug

Re-audited every single place a `FoodLogEntry` gets created (four call
sites) line by line before touching anything else. All four already
use the correct local-timezone-aware logic from earlier fixes -- no
remaining UTC-comparison bug found in creation or in the day-grouping
display logic, which was independently re-verified again too.

Proved directly why the problem can still show up anyway: an entry
created *before* today's timezone fixes landed -- which describes
everything logged during tonight's extensive testing -- was stamped
with a genuinely wrong `logged_at` value at the moment it was created.
Fixing the *display* logic afterward can't retroactively repair a
timestamp that was already wrong when it was written to the database;
the grouping code can only correctly bucket whatever's actually stored,
and for old rows, what's stored is still wrong. Confirmed this
mechanically: seeded a row with a deliberately bad timestamp alongside
a freshly-created correct one, and both landed in the same "Today"
group, because the display code has no way to know one of them was
wrong at creation.

Added a concrete way to verify this without taking it on faith: the
"Today" header now also shows the actual calendar date next to it
(`Today · Sun, Jul 12`), so it's directly checkable against a real
calendar rather than trusting a generic label. Old, mistimed entries
from before the fix can be cleaned up two ways now that they're clearly
labeled: delete and re-log (new entries use the corrected logic), or
ask the assistant directly to move a specific entry to the right time
-- the date/time correction capability built earlier tonight applies
here too.

## Chronological ordering within days, edit modal now includes date/time

**Entries within a day were displaying in reverse (latest-first)** --
matches the screenshot exactly: 11:42 AM dinner at the top, 7:35 AM
lunch at the bottom. The day-grouping query fetches everything in
descending order (needed so the *days themselves* show most-recent
first), but that left each day's own entries backwards unless
explicitly re-sorted afterward. Fixed for both Recent Timeline and
Exercise History -- days still show newest-first, but within each day,
entries now read chronologically (morning to evening), matching how a
day is actually read. Verified directly with entries at 5:18pm, 6:42pm,
and 2:35pm and confirmed they render in true time order, not insertion
order.

**The pencil-edit is a real form now**, not a single `prompt()` for
calories. Clicking it opens a modal with calories, date, and time
together, and it's no longer gated to AI-estimated entries only --
every entry can have its date/time corrected now, not just the ones
with a guessed calorie count. Backend accepts any combination of the
three (calories-only, date/time-only, or all together) and leaves
whichever fields weren't touched exactly as they were. Verified all
three modes directly: a date/time-only correction that left the
calorie value untouched, a combined edit changing all three at once,
and a malformed date rejected cleanly with a real error instead of a
crash.

## Assistant no longer lies about photo support, plus auto-prompt for new items

**Real bug, not a missing feature.** The assistant told the user photos
"aren't stored on log entries" when asked to remove one -- flatly
false, the photo-attachment feature has existed for hours. It said
this because it genuinely had zero visibility into `FoodLogPhoto`
records, so it confabulated an explanation instead of admitting it
didn't know. Fixed by adding photo IDs to each entry in the shared
context, extending the tool schema with a real `photo_deletions`
action, and being explicit in the system prompt that photos ARE
supported -- while also being honest that the assistant can't actually
see what a photo looks like from just a URL, so it should ask which
position rather than guess when someone describes one by appearance
and there's more than one on an entry. Caught a second bug while
testing this: the new `deleted_photos` field was making it into the
job result but never into the actual API response the frontend reads
-- same "added a field, forgot to wire it into the response builder"
mistake as an earlier fix tonight. Confirmed end-to-end after both
fixes: the assistant sees photo counts/ids correctly, and can actually
remove a specific one when asked.

**New: logging something with no photo yet now offers to add one
immediately**, per direct request. After a successful log via the
inline assistant, any brand-new item with no photo (not from photo
memory, not a meal photo upload) opens the photo picker right there,
one item at a time if several need one, before finally reloading.
Confirmed the two related pitfalls don't happen: an item that already
got a photo from memory doesn't get prompted again, and a meal-photo-
logged entry (which has a photo through a different field entirely,
not the manually-attached-photos list) doesn't get wrongly flagged as
needing one either.

## Splash screen now shows on every load, not just once per session

Removed the "only show once per browser session" logic entirely, per
direct request -- both the inline script that hid it before paint if
already dismissed, and the JS that remembered the dismissal. Tapping
still fades it away for that page view, it just no longer sticks
across future loads. Confirmed the page renders with zero
`sessionStorage` references left tied to the splash.

## Entry and activity names now display in title case

Added a display-only title-case formatter -- every word capitalized
except prepositions (which stay lowercase unless they're the first
word) -- applied to food entry names and exercise activity names
wherever they're shown on the Dashboard. Only touches display, never
rewrites what's actually stored. Tested against every example from the
request directly: "Jersey Mike's Hot Pepper spread (4 tbsp total (2
tbsp each sandwich))" correctly becomes "Jersey Mike's Hot Pepper
Spread (4 Tbsp Total (2 Tbsp Each Sandwich))" -- prepositions ("each")
stay lowercase, numbers and "%" are left alone, and the possessive
apostrophe in "Mike's" survives correctly.

## New: On This Day + Patriots News, back-burner items finally built

Both were flagged as pending from the very start of this project and
never got to. Verified both sources actually exist and work before
writing any app code: Wikimedia's public "On this day" REST API (no
key needed) and Boston.com's Patriots RSS feed, fetched directly and
confirmed real, live content came back for both.

Built with the same hard rule every other external call in this app
follows: never fetch inline in a request handler, since DigitalOcean
kills blocking requests. Instead it's a simple cache a background
thread keeps warm -- the route always returns instantly with whatever's
cached, kicking off a refresh in the background only when it notices
the cache is stale (a new day for On This Day, more than 2 hours old
for Patriots news). Confirmed directly that the route never blocks even
on the very first, cold-cache request, and that a warm cache produces
zero redundant fetches across repeated requests.

New Dashboard section, styled with the app's signature gradient accent
rather than looking bolted-on. On This Day shows 5 historical events
for today's actual date (randomized pick each day, linking to the
Wikipedia article where available); Patriots News shows the latest
headlines as clickable links. Both use stdlib-only parsing (no new
dependencies) and fail gracefully with a real message if a source is
temporarily down, rather than leaving a blank space or crashing the
page.

## On This Day / Patriots News moved into the hero row, and On This Day is now a mystery-year button

Restructured per direct request. Found the natural home for this
myself first: the hero gradient tile already had `max-width: 680px;
margin: 0 auto`, leaving equal empty space on both sides on a wide
screen -- exactly where these belonged. Wrapped it in a 3-column row
(Mystery Year / hero / Patriots News) that collapses back to a single
stacked column under ~1180px so it doesn't get cramped on anything
narrower than a wide desktop.

**Mystery Year**: On This Day's list and links are gone, replaced with
a single button that reveals one random year from today's actual pool
of historical events, tracked so it won't repeat a year until every
one in the pool has had a turn, then starts a fresh round automatically
rather than just stopping. Widened the backend fetch to the full
deduplicated set of years for a given date (confirmed 47 distinct years
on the day this was built, not just the 5 that used to show) instead of
a random subset, so there's an actual pool to draw from. Verified the
no-repeat logic by literally running it -- 3 reveals from a 3-year test
pool came back as all 3 distinct years in some order, and the 4th
reveal correctly reset and continued rather than getting stuck.

**Patriots News** stays a list, per direct request (only On This Day
became the mystery button) -- shows each headline's date alongside its
link. Added a football emoji next to the header as a stand-in for a
logo -- the real Patriots logo is trademarked NFL team IP, not
something safe to reproduce here, so this is the closest tasteful
equivalent rather than skipping the ask or doing something risky.

Caught and fixed a small pre-existing bug while working in this area:
the old extras section's loading text literally said `\u2026` on
screen -- an escape sequence written into an HTML file where it was
never going to be interpreted, not an actual ellipsis character. Moot
now since that whole section was replaced, but worth noting since it
had been silently wrong since it shipped.

## Popup windows, no more explanatory text, instant reveals every time

**Links now open as real popup windows**, not new tabs -- per direct
request, closing the popup leaves the original WeighTrack tab exactly
where it was. Applies to both the Mystery Year "Read more" link and
every Patriots headline.

**Removed the intro and progress text entirely** ("47 distinct years
to draw from today...", "1 of 47 years revealed today") -- the
no-repeat-until-exhausted mechanic still works exactly the same
underneath, it's just not narrated on screen anymore.

**Every click is now instant**, including the very first one after the
pool loads. Reworked the logic so the *next* year to reveal is always
pre-selected the moment the current one is shown, rather than being
picked fresh on each click -- by the time someone clicks again, there's
nothing left to compute, it's already sitting there ready. Verified
this directly rather than assuming it: confirmed a pick is already
prepped immediately after the pool loads, before every one of several
rapid clicks, and immediately after the pool resets once every year's
had a turn.

## Mystery Year reveals now include a matching image

Checked what Wikipedia's on-this-day API actually returns before
building anything: each event's linked article often carries a real
thumbnail image (`pages[0].thumbnail.source`), specific to that event
-- confirmed 41 of 47 events had one on a live test run. Wired that
through: the reveal now shows that image alongside the year and
description when one exists, and simply omits it (no broken-image
icon, no layout gap) for the handful of events that don't have one.

## Corrected: year graphic, not a Wikipedia photo, plus a real layout fix

Reverted the Wikipedia thumbnail entirely -- wrong interpretation of
"matching graphic." Built an actual year graphic instead: the year now
renders inside a gradient pill/medallion (matching the same pill shape
already used for the button elsewhere on this card), not plain colored
text and not a photo.

**Found the real cause of the dead space and narrow columns**: the
whole page was capped at `max-width: 1100px`, so once the 680px hero
tile took its share, the two side columns were fighting over roughly
190px each -- explains the text wrapping awkwardly in the screenshots.
Widened the page container to 1500px specifically so those columns
have real room (other pages just get a bit more breathing room on a
wide screen, doesn't hurt them). Also found why the cards had so much
empty space underneath their content: `align-items: stretch` was
forcing both side cards to match the *hero's* height, not their own.
Changed to `align-items: start` so each card is only as tall as what's
actually in it.

## Hero tile split into two, Mystery Year restyled, per the approved mockup

Built exactly what was mocked up and approved: the center gradient tile
is now two stacked tiles -- the top holds just "Day N" and one large,
centered Lbs Lost figure (the most prominent number on the page now),
the bottom holds Current/Goal/BMI enlarged in a 3-column row. Mystery
Year's title and revealed year are now centered (the year back to
plain gradient-clipped text, matching the reference image, not the
pill/badge treatment from the previous session); the event description
stays left-aligned underneath, as requested.

Checked two things that turned out to already be correctly built from
earlier in this session rather than needing new work: Patriots News was
already capped at 4 items, and the daily-refresh guarantee for
Patriots (a new calendar day always forces a fresh fetch, not just the
2-hour timer) was already in place. Didn't just trust that and move on
though -- wrote a real test simulating a fetch that's recent by the
clock but from the previous calendar day, run the way production
actually calls it (inside a request context), and confirmed the
refresh fires correctly.

## Fixed: pill centering, auto-reveal on load, tightened spacing

**Pills weren't filling the second tile** -- traced it to the inner
grid keeping its own `max-width: 680px` while the tile it sat inside
could end up computing to a different actual width, leaving a visible
gradient gap on the right. Fixed by making the pills grid always match
its direct parent's width exactly (`width: 100%`) instead of relying on
a separately-computed max-width that could drift out of sync with the
tile's real rendered size.

**A year and its Wikipedia link now show automatically the moment the
page loads** -- no click required for the first one, matching what was
asked directly. The button now gets a different year each time after
that, which was already the underlying mechanic; it just needed to
fire once automatically instead of waiting for a first click.

**Tightened the Mystery Year card's spacing** -- most of the visible
empty space was actually the reveal area sitting empty until a click
happened, which the auto-reveal fix resolves on its own; trimmed
remaining margins on top of that so the card wraps its content
closely.

## Found the real bug behind the pills, plus a fuller year narrative

**The pills were never actually fixed last time** -- both attempts
changed the right values but ran into a genuine CSS cascade bug: the
override (`.mobile-hero__numbers--three`) and the base class
(`.mobile-hero__numbers`) had identical specificity, and the base class
happened to be defined *later* in the file -- so it silently won and
kept capping the width at 680px regardless of what the override said.
Fixed for good with a compound selector (`.mobile-hero__numbers.mobile-hero__numbers--three`)
that has strictly higher specificity, so it wins regardless of source
order. Confirmed this with the actual specificity math, not just by
eyeballing it again.

**Mystery Year now pulls a fuller narrative for the revealed year**,
not just the single on-this-day sentence -- verified Wikipedia's
year-page summary endpoint returns real paragraph-length content before
building anything around it. Fetched lazily, only for years actually
revealed (not the whole ~47-year pool upfront), cached permanently
since a year's summary doesn't change, and routed through the same
background-thread pattern as everything else so a request handler never
blocks on it. Confirmed the cache and de-duplication both work: a cold
year returns nothing immediately then real content moments later, and
three rapid requests for the same uncached year only trigger one actual
fetch.

## Real story content this time, and a guaranteed height cap

**Found genuine story content, not another wrong guess.** Found
partially-built code already in the file trying a *different* fix
(linking to the on-this-day event's specific Wikipedia article instead
of the year page) -- tested it against the real 1913 example before
trusting it, and it turned out to link to broad, tangential topics
("Kingdom of Serbia" as a country, not the actual siege event), which
isn't better than the boilerplate. What actually works, confirmed
directly: the year page's own "Events" section, a genuine month-by-
month list of what happened that year. Also handled a real edge case
found while testing across several years: small/ambiguous year numbers
(like "79") land on a Wikipedia disambiguation page instead of the
actual article -- detected and treated as no-narrative-available rather
than showing garbage.

**The height cap is now measured, not guessed.** The previous attempt
truncated the narrative text at a fixed 320 characters as a rough
proxy for "roughly matches Patriots News' height" -- replaced with
something that actually measures Patriots' real rendered height in the
browser and applies that as a hard cap to Mystery Year, with a fade-out
at the bottom instead of an abrupt cut. Re-syncs after every reveal and
on window resize, and only applies in the side-by-side desktop layout
-- the stacked mobile view lets each card size to its own content
instead, since there's nothing to align there.

## Fixed the mid-sentence cutoff -- text now fits exactly, not masked

The height cap itself was working correctly, but simply clipping
whatever text happened to be there (even with the fade) meant it could
cut off mid-sentence, mid-word, which reads as broken rather than
intentional. Replaced with real fitting logic: the narrative is added
back sentence by sentence, measuring the actual rendered height each
time, stopping at the last sentence that still fits -- so it always
ends on a complete thought. Verified the sentence-splitting and
accumulation logic directly against the real 1913 narrative text and
confirmed every possible cutoff point lands on real punctuation, never
mid-word. Removed the fade-out mask entirely since there's no longer
partially-hidden content to soften -- what's visible is the complete
fitted text, not a clipped fragment.

## "Read more" no longer leads somewhere unrelated to the day

That was a real design flaw, not bad luck -- the link was pulling
whatever Wikipedia's on-this-day API considered the "primary linked
article" for an event, which is very often a broad, tangential topic
(a country, a public figure's entire biography) with no real
connection to that specific day once actually clicked through -- the
Bashar al-Assad/Syria example was a direct instance of the same
tangential-link problem found earlier with a different event. Fixed by
linking to the year's own Wikipedia page instead (the same page the
narrative content already comes from) -- always directly relevant to
what's being shown, confirmed the link genuinely resolves. Simplified
the backend to match: it no longer even fetches or carries a
per-event link field that was never a good idea to expose in the UI.

## Verified the day rollover for real, not just re-read the code

Given tonight's track record, treated "make sure" as a real
requirement to prove, not just re-confirm the logic looks right on
paper. Actually simulated a day boundary end-to-end: warmed the cache
for today, then called the real refresh-check function the exact way
production calls it on a fresh visit, pretending it was tomorrow.
Confirmed the cache's date marker updated and, more importantly, that
the actual returned events were genuinely different content -- not
just a date label change while quietly still serving today's events.
Also checked the client-side "don't repeat a year" tracking
specifically: it's keyed by the calendar date, so a new day gets a
completely fresh, unused key automatically, with no stale exclusions
carried over from the day before.

## Four small, direct changes

- Recent Timeline's day groups now load collapsed by default -- each
  day's header is a real button with a chevron indicator, click to
  expand/collapse. Exercise History's day groups are a separate
  component and were deliberately left untouched, matching the request.
- "Mystery Year" renamed to "On This Day..."
- "Calorie Target" -> "Daily Calorie Target"
- "Consumed" -> "Consumed Today", "Burned" -> "Burned Today"

## Real bug: narrative was always pulling January, regardless of the actual date

Confirmed and fixed. The narrative extraction was hardcoded to always
search from "=== January" in the year page's Events section, no matter
what date was actually being shown -- so revealing a year on July 13
would show January content from that year, completely unrelated to the
day actually highlighted. Fixed to search from the *current* month's
own section instead, and go a step further: it now tries to anchor
directly at the specific day within that month first (searched only
within a bounded window right after the month section starts, so a
coincidental date mention elsewhere in the article doesn't get picked
up by mistake). Verified directly against the real 1913 example: the
narrative now starts precisely at "July 13 -- The 1913 Romanian Army
cholera outbreak..." instead of January's Balkan War content having
nothing to do with the day shown.

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

- "On This Day" (Wikipedia) + Patriots RSS feed
- USDA FoodData Central fallback (needs a free API key, not yet obtained)
- GitHub repo + DigitalOcean App Platform deployment (in progress --
  app is live, see deployment notes above)
