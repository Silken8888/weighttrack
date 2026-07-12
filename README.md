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
