import os
import re
import io
import json
import base64
import time
import uuid
import threading
from queue import Queue, Empty
from datetime import datetime, timedelta

import requests
from flask import Flask, render_template, request, jsonify, abort
from PIL import Image, UnidentifiedImageError

# pyzbar's only mechanism for finding the zbar shared library on Linux is
# ctypes.util.find_library('zbar'), which searches the system's ldconfig
# cache -- not the filesystem directly. Confirmed live: DigitalOcean's
# Aptfile buildpack (heroku-buildpack-apt) installs libzbar0 into an
# internal layer without ever running ldconfig to register it, so
# find_library returns None even though the file genuinely exists on
# disk. This is a different, earlier-stage failure than the tesseract
# dependency issue from earlier -- no amount of adding libzbar0's own
# dependencies to the Aptfile could ever have fixed this, since the
# library is never even located in the first place, let alone loaded.
#
# Patch find_library to fall back to a direct filesystem search when
# asked for "zbar" specifically, before pyzbar's import-time load runs.
# Restored immediately after so this doesn't affect any other library
# lookup elsewhere in the app or its dependencies.
import ctypes.util as _ctypes_util
import glob as _glob

_real_find_library = _ctypes_util.find_library


def _find_library_with_zbar_fallback(name):
    if name == "zbar":
        found = _real_find_library(name)
        if found:
            return found
        candidates = (
            _glob.glob("/usr/lib/*/libzbar.so*")
            + _glob.glob("/usr/lib/libzbar.so*")
            + _glob.glob("/usr/local/lib/*/libzbar.so*")
            + _glob.glob("/layers/*/apt/usr/lib/*/libzbar.so*")
            + _glob.glob("/layers/**/libzbar.so*", recursive=True)
        )
        if candidates:
            print(f"INFO: ldconfig didn't know about libzbar, found it directly at {candidates[0]}")
            return candidates[0]
    return _real_find_library(name)


_ctypes_util.find_library = _find_library_with_zbar_fallback
try:
    from pyzbar.pyzbar import decode as decode_barcodes
    BARCODE_DECODING_AVAILABLE = True
except (ImportError, OSError) as exc:
    decode_barcodes = None
    BARCODE_DECODING_AVAILABLE = False
    print(f"WARNING: barcode decoding unavailable ({exc})")
finally:
    _ctypes_util.find_library = _real_find_library

from config import config_by_name
from models import (
    db, FoodItem, FoodLogEntry, MEAL_TYPES,
    WeighIn, VacationPeriod, UserProfile, ExerciseEntry, ACTIVITY_LEVELS,
)


def create_app(config_name=None):
    config_name = config_name or os.environ.get("FLASK_ENV", "development")
    app = Flask(__name__)
    app.config.from_object(config_by_name[config_name])

    db.init_app(app)
    with app.app_context():
        db.create_all()

    register_routes(app)
    start_search_worker(app)

    return app


# ---------------------------------------------------------------------------
# Background search worker
#
# Hard-won lesson from tonight's WannaPeek work: never let a request
# handler wait on a live external network call, not even a click-triggered
# one -- platform request timeouts don't care who's waiting. So
# POST /food/search only ever enqueues a job and returns a job_id
# immediately; the actual Open Food Facts call happens on a background
# thread, and the frontend polls GET /food/search/status/<job_id> until
# it's done. Routes only ever read the cache (_jobs); they never call
# requests.get() themselves.
#
# This is a personal, single-user app running as a single process, so an
# in-memory dict + Queue is enough -- no Redis/Celery needed. If this ever
# runs with more than one worker process, _jobs needs to move to a shared
# store (a Postgres table, or Redis), since each process would otherwise
# keep its own separate queue and never see the others' jobs.
# ---------------------------------------------------------------------------

_search_queue = Queue()
_jobs = {}
_jobs_lock = threading.Lock()


def _prune_old_jobs(ttl_seconds):
    cutoff = datetime.utcnow() - timedelta(seconds=ttl_seconds)
    with _jobs_lock:
        stale = [jid for jid, job in _jobs.items() if job["created_at"] < cutoff]
        for jid in stale:
            del _jobs[jid]


def _fetch_from_open_food_facts(app, query, page_size, retry_count_override=None):
    """One search attempt against Open Food Facts, with one retry on the
    transient 502/503s seen during testing (confirmed to clear within
    seconds). Always sends a real User-Agent -- Open Food Facts throttles
    or rejects requests without one.

    retry_count_override lets a caller (namely _progressive_search) run
    a candidate with fewer retries than the configured default -- used
    to bound total worst-case time across a multi-candidate cascade.
    """
    headers = {"User-Agent": app.config["OFF_USER_AGENT"]}
    params = {
        "search_terms": query,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": page_size,
    }

    retry_count = app.config["OFF_RETRY_COUNT"] if retry_count_override is None else retry_count_override
    attempts = retry_count + 1
    last_error = None

    for attempt in range(attempts):
        try:
            resp = requests.get(
                app.config["OFF_SEARCH_URL"],
                params=params,
                headers=headers,
                timeout=app.config["OFF_REQUEST_TIMEOUT_SECONDS"],
            )
            if resp.status_code in (502, 503):
                last_error = f"Open Food Facts returned {resp.status_code}"
                if attempt < attempts - 1:
                    time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
                continue
            resp.raise_for_status()
            return resp.json(), None
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < attempts - 1:
                time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
            continue

    return None, last_error or "Open Food Facts request failed"


# A trailing size/quantity phrase (from a label, or typed by hand) reliably
# kills an Open Food Facts match -- confirmed live: "Starbucks Caramel
# Macchiato Non-Dairy Creamer, 28 fl. oz." returns 0 results; stripping the
# ", 28 fl. oz." gets nothing on its own either, because "Non-Dairy" isn't
# in the product_name field OFF matches against. So this does two things:
# strips trailing units, and (below, in _progressive_search) progressively
# drops trailing words until something hits.
_SIZE_PATTERN = re.compile(
    r"[\s,]*\(?\b\d+(\.\d+)?\s*"
    r"(fl\.?\s*oz\.?|oz\.?|ounces?|lbs?\.?|pounds?|ml|milliliters?|l\.?|liters?|"
    r"g\.?|grams?|kg|kilograms?|ct\.?|count|pack|pk)\b\)?\.?\s*$",
    re.IGNORECASE,
)


def _clean_query(text):
    text = (text or "").strip()
    prev = None
    while prev != text:
        prev = text
        text = _SIZE_PATTERN.sub("", text).strip().rstrip(",.")
    return text


def _progressive_search(app, raw_query, page_size):
    """Search Open Food Facts, and if the full (cleaned) query comes back
    empty, retry with progressively fewer trailing words. Confirmed live:
    "Starbucks Caramel Macchiato Non-Dairy Creamer" -> 0 results, but
    "Starbucks Caramel Macchiato" -> 58, including the exact product.
    Stops at the first non-empty result set.

    Two things bound total worst-case time, both hit live: only the
    first (full-length, most-likely-correct) candidate gets the
    retry-on-502/503 treatment from _fetch_from_open_food_facts;
    fallback candidates get exactly one attempt each and move on
    immediately on failure, since with several fallback candidates
    available they act as their own retries via query variation. And the
    number of candidates tried is capped (not one-word-at-a-time down to
    the floor) -- a long OCR-derived guess (up to 12 words) could
    otherwise generate 10+ candidates, which combined with per-candidate
    retries pushed total processing time well past the client's ~20s
    poll timeout. Confirmed this exact failure live and fixed it here
    rather than just telling the user to wait longer.

    Returns (products_raw, query_used, error).
    """
    cleaned = _clean_query(raw_query)
    words = cleaned.split()
    if not words:
        return [], cleaned, None

    # Full length, then a handful of shorter candidates down to a floor
    # of 2 words -- capped at MAX_CANDIDATES total regardless of how many
    # words the query started with.
    MAX_CANDIDATES = 4
    word_counts = [len(words)]
    if len(words) > 2:
        step = max(1, (len(words) - 2) // (MAX_CANDIDATES - 1)) if MAX_CANDIDATES > 1 else len(words) - 2
        wc = len(words) - step
        while wc >= 2 and len(word_counts) < MAX_CANDIDATES:
            word_counts.append(wc)
            wc -= step
        if word_counts[-1] != 2 and len(word_counts) < MAX_CANDIDATES:
            word_counts.append(2)

    tried = []
    last_error = None
    got_clean_response = False
    for i, word_count in enumerate(word_counts):
        candidate = " ".join(words[:word_count])
        if candidate in tried:
            continue
        tried.append(candidate)

        # Only the first (longest/most likely correct) candidate gets
        # retried on a transient error -- fallback candidates fail fast
        # and move on, since trying the next candidate IS the retry.
        retry_override = None if i == 0 else 0
        data, error = _fetch_from_open_food_facts(app, candidate, page_size, retry_count_override=retry_override)
        if error:
            last_error = error
            continue
        got_clean_response = True
        products = data.get("products", [])
        if products:
            return products, candidate, None

    if not got_clean_response:
        # Every single candidate failed -- the search never actually
        # completed, so "no matches" would be a misleading thing to tell
        # the user (it looks like the product isn't in the database, when
        # really Open Food Facts just never returned a clean response).
        return None, tried[-1] if tried else cleaned, last_error

    return [], cleaned, None


def _fetch_product_by_barcode(app, barcode):
    """Exact-match lookup for a decoded barcode -- far more reliable than
    text search when we actually have one. Same one-retry-on-502/503
    pattern as the search endpoint.
    """
    headers = {"User-Agent": app.config["OFF_USER_AGENT"]}
    url = app.config["OFF_PRODUCT_URL"].format(barcode=barcode)

    attempts = app.config["OFF_RETRY_COUNT"] + 1
    last_error = None

    for attempt in range(attempts):
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=app.config["OFF_REQUEST_TIMEOUT_SECONDS"],
            )
            if resp.status_code in (502, 503):
                last_error = f"Open Food Facts returned {resp.status_code}"
                time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
                continue
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") == 1 and body.get("product"):
                return body["product"], None
            return None, None  # valid response, just no product for that barcode
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
            continue

    return None, last_error or "Open Food Facts request failed"


def _decode_barcode_from_bytes(image_bytes):
    """Local, no network -- try to read a UPC/EAN barcode straight off the
    photo. This is the primary path for packaged products: far more
    reliable than OCR or fuzzy text search, since it's an exact lookup.
    Returns the first decoded barcode string, or None. Returns None
    immediately (no-op) if the zbar shared library wasn't loadable at
    startup -- see the import guard above.
    """
    if not BARCODE_DECODING_AVAILABLE:
        return None

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except UnidentifiedImageError:
        return None

    results = decode_barcodes(img)
    if not results:
        # Open Food Facts photos (and phone photos) are sometimes higher
        # resolution than zbar wants -- a quick downscale occasionally
        # picks up a barcode the first pass missed.
        img.thumbnail((1200, 1200))
        results = decode_barcodes(img)

    for r in results:
        try:
            return r.data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _upload_to_bunny(app, image_bytes, filename):
    """Upload a meal photo to Bunny.net storage and return its public
    (Pull Zone) URL. Barcode decode is local (no network); this one
    genuinely needs it, so it gets the same one-retry-on-5xx treatment
    as the Open Food Facts calls.
    """
    zone = app.config["BUNNY_STORAGE_ZONE"]
    api_key = app.config["BUNNY_STORAGE_API_KEY"]
    pull_host = app.config["BUNNY_PULL_ZONE_HOST"]
    if not (zone and api_key and pull_host):
        return None, "Photo storage isn't configured yet -- missing Bunny.net settings."

    upload_url = f"https://{app.config['BUNNY_STORAGE_HOST']}/{zone}/meals/{filename}"
    headers = {"AccessKey": api_key, "Content-Type": "application/octet-stream"}

    attempts = app.config["OFF_RETRY_COUNT"] + 1
    last_error = None
    for attempt in range(attempts):
        try:
            resp = requests.put(
                upload_url,
                headers=headers,
                data=image_bytes,
                timeout=app.config["OFF_REQUEST_TIMEOUT_SECONDS"],
            )
            if resp.status_code in (502, 503):
                last_error = f"Bunny.net returned {resp.status_code}"
                time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
                continue
            resp.raise_for_status()
            return f"https://{pull_host}/meals/{filename}", None
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
            continue

    return None, last_error or "Couldn't upload the photo to storage"


def _resize_for_ai(image_bytes, max_edge=1024):
    """Downscale before sending to the API -- per Claude's vision guidance,
    images are resized server-side past ~1568px anyway, and a smaller
    upload means lower latency and fewer tokens for a task that only
    needs a rough estimate."""
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _call_claude(app, payload, timeout_multiplier=2):
    """Shared POST to the Anthropic Messages API with the same
    one-retry-on-5xx pattern used everywhere else in this file. Returns
    (data, error) -- data is the parsed JSON response body on success.
    """
    if not app.config["ANTHROPIC_API_KEY"]:
        return None, "This needs ANTHROPIC_API_KEY set -- not configured yet."

    headers = {
        "x-api-key": app.config["ANTHROPIC_API_KEY"],
        "anthropic-version": app.config["ANTHROPIC_VERSION"],
        "content-type": "application/json",
    }

    attempts = app.config["OFF_RETRY_COUNT"] + 1
    last_error = None
    for attempt in range(attempts):
        try:
            resp = requests.post(
                app.config["ANTHROPIC_API_URL"],
                headers=headers,
                json=payload,
                timeout=app.config["OFF_REQUEST_TIMEOUT_SECONDS"] * timeout_multiplier,
            )
            if resp.status_code in (502, 503, 529):
                last_error = f"Anthropic API returned {resp.status_code}"
                time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
                continue
            resp.raise_for_status()
            return resp.json(), None
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
            continue

    return None, last_error or "Couldn't reach the Anthropic API"


def _extract_claude_text(data):
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def _parse_json_from_claude(raw_text):
    cleaned = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
    return json.loads(cleaned)


def _estimate_meal_calories(app, image_bytes):
    """Ask Claude for a rough calorie estimate + short description of a
    meal photo. Returns (calories, description, error) -- calories/
    description are None on failure, error is None on success.
    """
    try:
        resized = _resize_for_ai(image_bytes)
    except UnidentifiedImageError:
        return None, None, "That doesn't look like a readable image."

    b64 = base64.standard_b64encode(resized).decode("utf-8")
    payload = {
        "model": app.config["ANTHROPIC_MEAL_MODEL"],
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": (
                    "Look at this photo of a meal. Give your single best rough "
                    "calorie estimate for personal food tracking (this is not "
                    "medical or nutritional advice, just a ballpark), plus a short "
                    "plain description under 8 words. Respond with ONLY a JSON "
                    'object and nothing else, no markdown fences: '
                    '{"calories": <integer>, "description": "<text>"}'
                )},
            ],
        }],
    }

    data, error = _call_claude(app, payload)
    if data is None:
        return None, None, error

    raw_text = _extract_claude_text(data)
    try:
        parsed = _parse_json_from_claude(raw_text)
        calories = int(parsed["calories"]) if parsed.get("calories") is not None else None
        description = (parsed.get("description") or "").strip()[:200] or None
        return calories, description, None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        # Last-resort fallback: pull the first number out of whatever came
        # back rather than losing the estimate entirely over a formatting slip.
        match = re.search(r"\d+", raw_text)
        if match:
            return int(match.group()), None, None
        return None, None, "Got a response from the AI but couldn't read a calorie number from it."


def _normalize_product(raw):
    """Map one Open Food Facts product into the shape our FoodItem form
    expects.

    Per-serving nutrition is inconsistently populated -- confirmed with
    real products: Nutella only had per-100g data, Cheerios had full
    per-serving data. So we prefer *_serving fields when present and fall
    back to *_100g, and report which basis we used ("serving" vs "100g")
    so the UI can ask the user to confirm a real serving size and scale
    when we had to fall back.
    """
    nutriments = raw.get("nutriments", {}) or {}

    def pick(per_serving_key, per_100g_key):
        if per_serving_key in nutriments and nutriments[per_serving_key] is not None:
            return nutriments[per_serving_key], "serving"
        if per_100g_key in nutriments and nutriments[per_100g_key] is not None:
            return nutriments[per_100g_key], "100g"
        return None, None

    calories, cal_basis = pick("energy-kcal_serving", "energy-kcal_100g")
    protein, _ = pick("proteins_serving", "proteins_100g")
    carbs, _ = pick("carbohydrates_serving", "carbohydrates_100g")
    fat, _ = pick("fat_serving", "fat_100g")
    fiber, _ = pick("fiber_serving", "fiber_100g")
    sugar, _ = pick("sugars_serving", "sugars_100g")
    sodium_g, _ = pick("sodium_serving", "sodium_100g")

    # Open Food Facts reports sodium in grams; nutrition labels use mg.
    sodium_mg = round(sodium_g * 1000, 1) if sodium_g is not None else None

    return {
        "product_name": raw.get("product_name") or "Unknown product",
        "brand": raw.get("brands"),
        "barcode": raw.get("code"),
        "photo_url": raw.get("image_front_url"),
        "serving_description": raw.get("serving_size"),
        "calories": calories,
        "protein_g": protein,
        "carbs_g": carbs,
        "fat_g": fat,
        "fiber_g": fiber,
        "sugar_g": sugar,
        "sodium_mg": sodium_mg,
        "basis": cal_basis,  # "serving" | "100g" | None
        "source": "open_food_facts",
    }


def _has_nutrition_data(normalized_product):
    return normalized_product.get("calories") is not None


def _rank_products(products):
    """Prefer results that actually have both a photo and real nutrition
    data. Open Food Facts is crowdsourced, so the same real-world product
    commonly exists as multiple separate entries under different
    barcodes with wildly different completeness -- confirmed live: a
    Starbucks Hazelnut Latte creamer exists as two entries, one with
    full nutrition data, one with just a photo and nothing else. Sorting
    complete entries first means the user sees the good one before the
    sparse one, rather than confirming whichever OFF happened to list
    first.
    """
    return sorted(products, key=lambda p: (p.get("calories") is None, p.get("photo_url") is None))


def _run_text_job(app, job):
    products_raw, query_used, error = _progressive_search(app, job["query"], job["page_size"])
    if error:
        return None, error
    products = _rank_products([_normalize_product(p) for p in products_raw])
    note = None
    if query_used != _clean_query(job["query"]):
        note = f'No match for the full text -- broadened the search to "{query_used}".'
    return {"results": products, "match_type": "text", "note": note}, None


def _run_photo_job(app, job):
    image_bytes = job["image_bytes"]

    barcode = _decode_barcode_from_bytes(image_bytes)
    if barcode:
        product, error = _fetch_product_by_barcode(app, barcode)
        if error:
            return None, error
        if product:
            normalized = _normalize_product(product)
            results = [normalized]
            note = f"Read barcode {barcode} off the photo."

            if not _has_nutrition_data(normalized):
                # This exact barcode's OFF entry is missing nutrition
                # data -- a real, confirmed gap in their crowdsourced
                # data, not a bug in how this app reads it. Try a
                # supplementary search so there's a better entry to pick
                # from if one exists.
                #
                # Searching the *full* product name doesn't work here --
                # confirmed live it just re-matches this same sparse
                # entry by its own exact name and never surfaces a
                # sibling. A shorter brand + first-few-words query does:
                # confirmed live it found both this entry and a complete
                # duplicate under a different barcode.
                short_query = " ".join(
                    filter(None, [normalized.get("brand"), *normalized["product_name"].split()[:3]])
                )
                extra_raw, _, extra_error = _progressive_search(app, short_query, 5)
                if not extra_error and extra_raw:
                    extras = [_normalize_product(p) for p in extra_raw if p.get("code") != barcode]
                    if extras:
                        results = _rank_products(results + extras)
                        note += (
                            " That exact listing is missing nutrition data on Open Food Facts -- "
                            "found a more complete match too, ranked first below."
                        )

            return {"results": results, "match_type": "barcode", "note": note}, None
        # Decoded a barcode, but Open Food Facts has no record of it.
        return {
            "results": [],
            "match_type": "none",
            "note": f"Read barcode {barcode} off the photo, but Open Food Facts has no record of it. Try the text search above.",
        }, None

    # No barcode found at all. This app intentionally does not fall back
    # to OCR-based label reading here -- that path caused a long chain of
    # real, hard-to-predict failures (garbled text, hyphenated words
    # silently breaking Open Food Facts' search, sparse duplicate
    # entries) with no clean fix. Barcode-only is slower to set up (you
    # need a legible barcode in the shot) but exact when it works.
    return {
        "results": [],
        "match_type": "none",
        "note": "Couldn't find a barcode in that photo. Try a clearer, closer shot of the barcode, or use the text search above.",
    }, None


def _run_meal_photo_job(app, job):
    """Upload a meal photo to Bunny.net, ask Claude for a rough calorie
    estimate, and create the FoodLogEntry directly (unlike food search,
    there's no candidate list to confirm -- one photo becomes one entry,
    and the user adjusts the calorie number afterward if the AI guessed
    wrong).
    """
    image_bytes = job["image_bytes"]
    filename = f"{uuid.uuid4().hex}.jpg"

    photo_url, error = _upload_to_bunny(app, image_bytes, filename)
    if error:
        return None, error

    ai_calories, ai_description, ai_error = _estimate_meal_calories(app, image_bytes)
    # An AI-estimate failure shouldn't discard a photo that uploaded fine
    # -- save the entry anyway with calories left blank so the user can
    # fill it in themselves, and surface the AI error as a note instead
    # of losing the whole log entry over it.

    with app.app_context():
        entry = FoodLogEntry(
            food_item_id=None,
            photo_url=photo_url,
            description=job.get("description") or ai_description,
            ai_calories=ai_calories,
            meal_type=job["meal_type"],
            servings=job.get("servings", 1.0),
            logged_at=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
        result = entry.to_dict()

    return {"entry": result, "note": ai_error}, None


def _run_food_agent(app, job):
    """Parse a free-text message like "two pieces of wheat toast with
    jif chunky peanut butter and 28 oz of coffee with 3 tbsp of
    starbucks non-dairy creamer" into distinct food items, each with its
    own estimated nutrition, and log each one directly -- no search, no
    confirmation step, matching how photo logging already works.

    meal_type comes from the dropdown the user picks before typing, not
    from Claude's guess -- removes a whole category of ambiguity ("was
    that lunch or a snack?") since the user already said which.
    """
    message = job["message"]
    meal_type = job["meal_type"]
    today = datetime.utcnow().date().isoformat()
    now_time = datetime.utcnow().strftime("%H:%M")

    payload = {
        "model": app.config["ANTHROPIC_AGENT_MODEL"],
        "max_tokens": 1500,
        "messages": [{
            "role": "user",
            "content": (
                f"Today's date is {today} (current time {now_time} UTC). "
                "You are a food logging assistant for a personal nutrition "
                f"tracker. The user has already told the app this is for "
                f"\"{meal_type}\" -- don't guess a different meal type, use "
                f"\"{meal_type}\" for every item. They'll describe what they "
                "ate, sometimes casually, sometimes for a past date. Break "
                "their message into distinct food/drink items (e.g. \"two "
                "pieces of wheat toast with jif chunky peanut butter\" is "
                "separate items for the toast and the peanut butter, since "
                "they have very different nutrition profiles). For each "
                "item, give your single best rough estimate of calories, "
                "protein (g), carbs (g), and fat (g) for the stated "
                "quantity -- these are estimates for personal tracking, not "
                "medical or nutritional advice. Infer the date from what "
                "they say (\"today\", \"yesterday\", a specific date, or "
                "unstated -> today), as an ISO date (YYYY-MM-DD). If the "
                "message isn't describing food at all (e.g. a question), "
                "return an empty items list and answer in the reply field "
                "instead.\n\n"
                f'User message: "{message}"\n\n'
                "Respond with ONLY a JSON object, no markdown fences, no "
                "other text:\n"
                '{"items": [{"name": "<short name>", "quantity": "<what '
                'they said, e.g. \'2 slices\'>", "date": "YYYY-MM-DD", '
                '"calories": <integer>, "protein_g": <number>, "carbs_g": '
                '<number>, "fat_g": <number>}, ...], "reply": "<a short, '
                "natural, friendly confirmation of what you logged and the "
                'total calories -- or your answer, if it wasn\'t a food '
                'message>"}'
            ),
        }],
    }

    data, error = _call_claude(app, payload, timeout_multiplier=3)
    if data is None:
        return None, error

    raw_text = _extract_claude_text(data)
    try:
        parsed = _parse_json_from_claude(raw_text)
    except json.JSONDecodeError:
        return None, "Got a response from the AI but couldn't parse it. Try rephrasing."

    items = parsed.get("items") or []
    reply = (parsed.get("reply") or "").strip() or "Logged."
    batch_id = uuid.uuid4().hex

    created = []
    with app.app_context():
        for item in items:
            try:
                try:
                    item_date = datetime.fromisoformat(item.get("date", today)).date()
                except (ValueError, TypeError):
                    item_date = datetime.utcnow().date()

                logged_at = (
                    datetime.utcnow()
                    if item_date == datetime.utcnow().date()
                    else datetime.combine(item_date, datetime.min.time().replace(hour=12))
                )

                name = (item.get("name") or "Food item").strip()[:200]
                quantity = (item.get("quantity") or "").strip()
                description = f"{name} ({quantity})" if quantity else name

                def _num(key):
                    val = item.get(key)
                    try:
                        return float(val) if val is not None else None
                    except (TypeError, ValueError):
                        return None

                entry = FoodLogEntry(
                    food_item_id=None,
                    description=description[:200],
                    meal_type=meal_type,
                    servings=1.0,
                    logged_at=logged_at,
                    ai_calories=_num("calories"),
                    ai_protein_g=_num("protein_g"),
                    ai_carbs_g=_num("carbs_g"),
                    ai_fat_g=_num("fat_g"),
                    batch_id=batch_id,
                )
                db.session.add(entry)
                created.append(entry)
            except Exception:  # noqa: BLE001 -- one malformed item shouldn't drop the rest
                continue

        db.session.commit()
        entries = [e.to_dict() for e in created]

    return {"reply": reply, "entries": entries}, None


def _search_worker(app):
    while True:
        try:
            job_id = _search_queue.get(timeout=1)
        except Empty:
            continue

        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:
            continue

        # Per-item try/except (not one try/except wrapping the whole loop)
        # so a single bad job can't take the worker thread down or get
        # skipped without ever marking its job as errored.
        try:
            if job["kind"] == "photo":
                outcome, error = _run_photo_job(app, job)
            elif job["kind"] == "meal_photo":
                outcome, error = _run_meal_photo_job(app, job)
            elif job["kind"] == "agent_message":
                outcome, error = _run_food_agent(app, job)
            else:
                outcome, error = _run_text_job(app, job)

            if error:
                with _jobs_lock:
                    _jobs[job_id].update(status="error", error=error)
                continue

            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id].update(outcome)
        except Exception as exc:  # noqa: BLE001 -- must never kill the worker thread
            with _jobs_lock:
                _jobs[job_id].update(status="error", error=str(exc))
        finally:
            _prune_old_jobs(app.config["SEARCH_JOB_TTL_SECONDS"])
            # image bytes only need to live long enough to be processed
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id].pop("image_bytes", None)


def start_search_worker(app):
    thread = threading.Thread(target=_search_worker, args=(app,), daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Weigh-in log helpers: streak (vacation-aware), rolling average, and a
# hand-rolled SVG sparkline -- no charting library needed for one line.
# ---------------------------------------------------------------------------

def _compute_streak(weigh_ins, vacation_periods):
    """Consecutive calendar days with a weigh-in, walking backward from
    today, treating vacation-covered days as grace days that count
    toward the streak without needing an entry -- per the original
    spec, a trip shouldn't zero out (or silently discount) an
    established streak. Today itself is allowed to be pending (not yet
    logged) without breaking anything, since the day isn't over.
    """
    if not weigh_ins:
        return 0
    logged_dates = {w.logged_at.date() for w in weigh_ins}

    def on_vacation(d):
        return any(vp.start_date <= d <= vp.end_date for vp in vacation_periods)

    streak = 0
    day = datetime.utcnow().date()
    if day not in logged_dates and not on_vacation(day):
        day -= timedelta(days=1)
    while day in logged_dates or on_vacation(day):
        streak += 1
        day -= timedelta(days=1)
    return streak


def _rolling_average(weigh_ins, as_of, window_days=7):
    window_start = as_of - timedelta(days=window_days - 1)
    values = [w.weight_lbs for w in weigh_ins if window_start <= w.logged_at.date() <= as_of]
    if not values:
        return None
    return sum(values) / len(values)


def _weigh_in_chart_svg(weigh_ins, days=30):
    """A 7-day rolling average line, not raw daily dots -- per the
    original spec, daily weight alone is too noisy to be useful. Plain
    inline SVG, no charting library.
    """
    if len(weigh_ins) < 2:
        return None

    today = datetime.utcnow().date()
    points = []
    for i in range(days, -1, -1):
        day = today - timedelta(days=i)
        avg = _rolling_average(weigh_ins, day)
        if avg is not None:
            points.append((day, avg))

    if len(points) < 2:
        return None

    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    width, height, pad = 600, 160, 12
    usable_w = width - 2 * pad
    usable_h = height - 2 * pad

    coords = []
    for i, (_, v) in enumerate(points):
        x = pad + (i / (len(points) - 1)) * usable_w
        y = pad + (1 - (v - lo) / span) * usable_h
        coords.append((round(x, 1), round(y, 1)))

    polyline_points = " ".join(f"{x},{y}" for x, y in coords)
    last_x, last_y = coords[-1]

    return {
        "polyline_points": polyline_points,
        "last_x": last_x,
        "last_y": last_y,
        "width": width,
        "height": height,
        "lo": round(lo, 1),
        "hi": round(hi, 1),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register_routes(app):

    def _all_food_items():
        return db.session.execute(
            db.select(FoodItem).order_by(FoodItem.nickname)
        ).scalars().all()

    def _todays_log_entries():
        # UTC day boundary -- see the note on FoodLogEntry.logged_at.
        today = datetime.utcnow().date()
        start = datetime.combine(today, datetime.min.time())
        end = start + timedelta(days=1)
        return db.session.execute(
            db.select(FoodLogEntry)
            .filter(FoodLogEntry.logged_at >= start, FoodLogEntry.logged_at < end)
            .order_by(FoodLogEntry.logged_at)
        ).scalars().all()

    def _all_weigh_ins():
        return db.session.execute(
            db.select(WeighIn).order_by(WeighIn.logged_at)
        ).scalars().all()

    def _all_vacation_periods():
        return db.session.execute(
            db.select(VacationPeriod).order_by(VacationPeriod.start_date)
        ).scalars().all()

    def _get_profile():
        profile = db.session.get(UserProfile, 1)
        if profile is None:
            profile = UserProfile(id=1)
            db.session.add(profile)
            db.session.commit()
        return profile

    def _food_page_context():
        entries = _todays_log_entries()
        # .calories (not .scaled("calories")) -- scaled() only works for
        # library-linked entries; photo-logged meals need the property to
        # pick up ai_calories/manual_calories. Using .scaled() directly
        # here was a real bug: photo-logged meals silently contributed 0
        # to today's total.
        total_calories = sum(e.calories or 0 for e in entries)

        weigh_ins = _all_weigh_ins()
        vacations = _all_vacation_periods()
        latest_weight = weigh_ins[-1].weight_lbs if weigh_ins else None
        streak = _compute_streak(weigh_ins, vacations)

        profile = _get_profile()
        calorie_target = profile.calorie_target(latest_weight) if latest_weight else None

        return {
            "items": _all_food_items(),
            "active_nav": "food",
            "log_entries": entries,
            "calories_today": round(total_calories) if entries else None,
            "latest_weight": latest_weight,
            "streak": streak,
            "calorie_target": calorie_target,
        }

    @app.route("/")
    def index():
        return render_template("food_library.html", **_food_page_context())

    @app.route("/food")
    def food_library():
        return render_template("food_library.html", **_food_page_context())

    @app.route("/log/add", methods=["POST"])
    def log_add():
        payload = request.get_json(silent=True) or {}

        food_item_id = payload.get("food_item_id")
        food_item = db.session.get(FoodItem, food_item_id) if food_item_id else None
        if food_item is None:
            return jsonify(error="Pick a food from your library first"), 400

        meal_type = (payload.get("meal_type") or "").strip().lower()
        if meal_type not in MEAL_TYPES:
            return jsonify(error="Meal type must be breakfast, lunch, dinner, or snack"), 400

        try:
            servings = float(payload.get("servings", 1) or 1)
        except (TypeError, ValueError):
            return jsonify(error="Servings must be a number"), 400
        if servings <= 0:
            return jsonify(error="Servings must be greater than zero"), 400

        logged_at = datetime.utcnow()
        raw_time = payload.get("logged_at")
        if raw_time:
            try:
                logged_at = datetime.fromisoformat(raw_time)
            except ValueError:
                pass  # fall back to now rather than reject the whole entry

        entry = FoodLogEntry(
            food_item_id=food_item.id,
            meal_type=meal_type,
            servings=servings,
            logged_at=logged_at,
        )
        db.session.add(entry)
        db.session.commit()
        return jsonify(entry.to_dict()), 201

    @app.route("/log/<int:entry_id>/delete", methods=["POST"])
    def log_delete(entry_id):
        entry = db.session.get(FoodLogEntry, entry_id)
        if entry is None:
            abort(404)
        db.session.delete(entry)
        db.session.commit()
        return jsonify(success=True)

    @app.route("/agent/message", methods=["POST"])
    def agent_message():
        """Natural-language food logging: pick a meal type from the
        dropdown, describe what you ate in plain language, and it
        becomes several distinct FoodLogEntry rows directly -- no
        search, no confirmation step. Same background-job pattern as
        everything else that calls an external API.
        """
        payload = request.get_json(silent=True) or {}
        message = (payload.get("message") or "").strip()
        if not message:
            return jsonify(error="Say something first"), 400
        if len(message) > 2000:
            return jsonify(error="That's a lot -- try breaking it into a shorter message"), 400

        meal_type = (payload.get("meal_type") or "").strip().lower()
        if meal_type not in MEAL_TYPES:
            return jsonify(error="Pick a meal type first"), 400

        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _jobs[job_id] = {
                "kind": "agent_message",
                "status": "pending",
                "message": message,
                "meal_type": meal_type,
                "created_at": datetime.utcnow(),
                "results": None,
                "error": None,
            }
        _search_queue.put(job_id)

        return jsonify(job_id=job_id), 202

    @app.route("/agent/recent-meals")
    def agent_recent_meals():
        """'Claude will learn and proactively offer up the meals in the
        past' -- no ML needed for this part, just the app's own history:
        the most recent distinct agent-logged batches for a meal type,
        so repeating yesterday's breakfast is one tap instead of typing
        it all out again.
        """
        meal_type = (request.args.get("meal_type") or "").strip().lower()
        if meal_type not in MEAL_TYPES:
            return jsonify(error="Invalid meal type"), 400

        recent_entries = db.session.execute(
            db.select(FoodLogEntry)
            .filter(FoodLogEntry.meal_type == meal_type, FoodLogEntry.batch_id.isnot(None))
            .order_by(FoodLogEntry.logged_at.desc())
            .limit(60)  # a handful of batches' worth, grouped below
        ).scalars().all()

        batches = {}
        order = []
        for e in recent_entries:
            if e.batch_id not in batches:
                batches[e.batch_id] = []
                order.append(e.batch_id)
            batches[e.batch_id].append(e)

        suggestions = []
        for batch_id in order[:5]:
            items = batches[batch_id]
            total_cal = sum(i.calories or 0 for i in items)
            suggestions.append({
                "batch_id": batch_id,
                "summary": ", ".join(i.display_name for i in items),
                "total_calories": round(total_cal),
                "item_count": len(items),
                "logged_at": items[0].logged_at.isoformat(),
            })

        return jsonify(suggestions=suggestions)

    @app.route("/agent/repeat", methods=["POST"])
    def agent_repeat():
        """Clone a past batch's items as new entries logged right now --
        no AI call needed, just duplicating known-good data.
        """
        payload = request.get_json(silent=True) or {}
        batch_id = (payload.get("batch_id") or "").strip()
        if not batch_id:
            return jsonify(error="Missing batch_id"), 400

        source_items = db.session.execute(
            db.select(FoodLogEntry).filter(FoodLogEntry.batch_id == batch_id)
        ).scalars().all()
        if not source_items:
            return jsonify(error="Couldn't find that meal to repeat"), 404

        new_batch_id = uuid.uuid4().hex
        created = []
        for src in source_items:
            entry = FoodLogEntry(
                food_item_id=None,
                description=src.description,
                meal_type=src.meal_type,
                servings=1.0,
                logged_at=datetime.utcnow(),
                ai_calories=src.ai_calories,
                ai_protein_g=src.ai_protein_g,
                ai_carbs_g=src.ai_carbs_g,
                ai_fat_g=src.ai_fat_g,
                batch_id=new_batch_id,
            )
            db.session.add(entry)
            created.append(entry)
        db.session.commit()

        return jsonify(entries=[e.to_dict() for e in created]), 201

    @app.route("/log/photo", methods=["POST"])
    def log_photo_start():
        """Snap a photo of a meal -- unlike /food/search-photo, this
        doesn't return candidates to confirm. One photo becomes one
        timeline entry directly; the calorie number is a starting guess
        the user can correct via /log/<id>/adjust.
        """
        photo = request.files.get("photo")
        if photo is None or photo.filename == "":
            return jsonify(error="Attach a photo first"), 400
        if photo.mimetype not in app.config["ALLOWED_PHOTO_MIMETYPES"]:
            return jsonify(error="Please upload a JPEG, PNG, or WEBP photo"), 400

        image_bytes = photo.read(app.config["MAX_MEAL_PHOTO_BYTES"] + 1)
        if len(image_bytes) > app.config["MAX_MEAL_PHOTO_BYTES"]:
            return jsonify(error="That photo is too large (8MB max)"), 400
        if not image_bytes:
            return jsonify(error="That photo looks empty -- try again"), 400

        meal_type = (request.form.get("meal_type") or "").strip().lower()
        if meal_type not in MEAL_TYPES:
            return jsonify(error="Meal type must be breakfast, lunch, dinner, or snack"), 400

        try:
            servings = float(request.form.get("servings", 1) or 1)
        except (TypeError, ValueError):
            servings = 1.0
        if servings <= 0:
            servings = 1.0

        description = (request.form.get("description") or "").strip() or None

        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _jobs[job_id] = {
                "kind": "meal_photo",
                "status": "pending",
                "image_bytes": image_bytes,
                "meal_type": meal_type,
                "servings": servings,
                "description": description,
                "created_at": datetime.utcnow(),
                "results": None,
                "error": None,
            }
        _search_queue.put(job_id)

        return jsonify(job_id=job_id), 202

    @app.route("/log/<int:entry_id>/adjust", methods=["POST"])
    def log_adjust(entry_id):
        entry = db.session.get(FoodLogEntry, entry_id)
        if entry is None:
            abort(404)

        payload = request.get_json(silent=True) or {}
        try:
            calories = float(payload.get("calories"))
        except (TypeError, ValueError):
            return jsonify(error="Enter a valid number of calories"), 400
        if calories < 0:
            return jsonify(error="Calories can't be negative"), 400

        entry.manual_calories = calories
        db.session.commit()
        return jsonify(entry.to_dict())

    @app.route("/food/search", methods=["POST"])
    def food_search_start():
        payload = request.get_json(silent=True) or {}
        query = (payload.get("query") or "").strip()
        if not query:
            return jsonify(error="Search term is required"), 400

        page_size = min(int(payload.get("page_size", 10)), 25)

        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _jobs[job_id] = {
                "kind": "text",
                "status": "pending",
                "query": query,
                "page_size": page_size,
                "created_at": datetime.utcnow(),
                "results": None,
                "error": None,
            }
        _search_queue.put(job_id)

        return jsonify(job_id=job_id), 202

    @app.route("/food/search-photo", methods=["POST"])
    def food_search_photo_start():
        """Upload a product photo instead of typing a search. We try a
        barcode first (exact, reliable) and fall back to OCR-ing the
        label text through the same search path as a typed query. Open
        Food Facts has no public reverse-image search, so a true "find
        this exact photo" match isn't possible -- this is the closest
        equivalent that's actually real.
        """
        photo = request.files.get("photo")
        if photo is None or photo.filename == "":
            return jsonify(error="Attach a photo first"), 400

        if photo.mimetype not in app.config["ALLOWED_PHOTO_MIMETYPES"]:
            return jsonify(error="Please upload a JPEG, PNG, or WEBP photo"), 400

        image_bytes = photo.read(app.config["MAX_PHOTO_BYTES"] + 1)
        if len(image_bytes) > app.config["MAX_PHOTO_BYTES"]:
            return jsonify(error="That photo is too large (8MB max)"), 400
        if not image_bytes:
            return jsonify(error="That photo looks empty -- try again"), 400

        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _jobs[job_id] = {
                "kind": "photo",
                "status": "pending",
                "image_bytes": image_bytes,
                "page_size": 10,
                "created_at": datetime.utcnow(),
                "results": None,
                "error": None,
            }
        _search_queue.put(job_id)

        return jsonify(job_id=job_id), 202

    @app.route("/food/search/status/<job_id>")
    def food_search_status(job_id):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:
            return jsonify(error="Unknown or expired search job"), 404

        response = {"status": job["status"]}
        if job["status"] == "done":
            if "results" in job:
                response["results"] = job["results"]
                response["match_type"] = job.get("match_type")
            if "entry" in job:
                response["entry"] = job["entry"]
            if "entries" in job:
                response["entries"] = job["entries"]
            if "reply" in job:
                response["reply"] = job["reply"]
            response["note"] = job.get("note")
        elif job["status"] == "error":
            response["error"] = job["error"]
        return jsonify(response)

    @app.route("/food/add", methods=["POST"])
    def food_add():
        payload = request.get_json(silent=True) or {}
        nickname = (payload.get("nickname") or "").strip()
        if not nickname:
            return jsonify(error="Give this food a nickname first"), 400

        existing = db.session.execute(
            db.select(FoodItem).filter_by(nickname=nickname)
        ).scalar_one_or_none()
        if existing:
            return jsonify(error="You already have a food saved under that nickname"), 409

        item = FoodItem(
            nickname=nickname,
            product_name=payload.get("product_name") or "Unknown product",
            brand=payload.get("brand"),
            barcode=payload.get("barcode"),
            photo_url=payload.get("photo_url"),
            serving_description=payload.get("serving_description"),
            calories=payload.get("calories"),
            protein_g=payload.get("protein_g"),
            carbs_g=payload.get("carbs_g"),
            fat_g=payload.get("fat_g"),
            fiber_g=payload.get("fiber_g"),
            sugar_g=payload.get("sugar_g"),
            sodium_mg=payload.get("sodium_mg"),
            source=payload.get("source", "open_food_facts"),
        )
        db.session.add(item)
        db.session.commit()
        return jsonify(item.to_dict()), 201

    @app.route("/food/<int:item_id>/delete", methods=["POST"])
    def food_delete(item_id):
        item = db.session.get(FoodItem, item_id)
        if item is None:
            abort(404)
        db.session.delete(item)
        db.session.commit()
        return jsonify(success=True)

    # -----------------------------------------------------------------
    # Weigh-In Log
    # -----------------------------------------------------------------

    @app.route("/weigh-in")
    def weigh_in_log():
        weigh_ins = _all_weigh_ins()
        vacations = _all_vacation_periods()
        streak = _compute_streak(weigh_ins, vacations)
        rolling_avg = _rolling_average(weigh_ins, datetime.utcnow().date()) if weigh_ins else None
        chart = _weigh_in_chart_svg(weigh_ins)

        milestones = []
        if len(weigh_ins) == 1:
            milestones.append("First weigh-in logged -- nice start.")
        if streak and streak % 7 == 0:
            milestones.append(f"{streak}-day streak!")
        if len(weigh_ins) >= 2:
            change = weigh_ins[-1].weight_lbs - weigh_ins[0].weight_lbs
            if abs(change) >= 5:
                direction = "down" if change < 0 else "up"
                milestones.append(f"{abs(round(change, 1))} lbs {direction} since your first entry.")

        return render_template(
            "weigh_in.html",
            active_nav="weigh_in",
            weigh_ins=list(reversed(weigh_ins)),
            streak=streak,
            rolling_avg=round(rolling_avg, 1) if rolling_avg is not None else None,
            chart=chart,
            milestones=milestones,
        )

    @app.route("/weigh-in/add", methods=["POST"])
    def weigh_in_add():
        payload = request.get_json(silent=True) or {}
        try:
            weight_lbs = float(payload.get("weight_lbs"))
        except (TypeError, ValueError):
            return jsonify(error="Enter a valid weight"), 400
        if weight_lbs <= 0 or weight_lbs > 1000:
            return jsonify(error="That doesn't look like a valid weight in lbs"), 400

        notes = (payload.get("notes") or "").strip() or None

        # Optional backdating -- if a date is given and it's not today,
        # use noon on that date (we only have a date, not a time, from
        # the form). Milestones and the "first entry" reference already
        # work off whichever logged_at is earliest, sorted at query time
        # -- not insertion order -- so backdating an earlier entry
        # automatically recalibrates "day one" without any extra logic.
        raw_date = (payload.get("date") or "").strip()
        logged_at = datetime.utcnow()
        if raw_date:
            try:
                entry_date = datetime.fromisoformat(raw_date).date()
                if entry_date != datetime.utcnow().date():
                    logged_at = datetime.combine(entry_date, datetime.min.time().replace(hour=12))
                if entry_date > datetime.utcnow().date():
                    return jsonify(error="Can't log a weigh-in in the future"), 400
            except ValueError:
                return jsonify(error="That date doesn't look right"), 400

        entry = WeighIn(weight_lbs=weight_lbs, notes=notes, logged_at=logged_at)
        db.session.add(entry)
        db.session.commit()
        return jsonify(entry.to_dict()), 201

    @app.route("/weigh-in/<int:entry_id>/delete", methods=["POST"])
    def weigh_in_delete(entry_id):
        entry = db.session.get(WeighIn, entry_id)
        if entry is None:
            abort(404)
        db.session.delete(entry)
        db.session.commit()
        return jsonify(success=True)

    # -----------------------------------------------------------------
    # Vacation Mode
    # -----------------------------------------------------------------

    @app.route("/vacation")
    def vacation_mode():
        today = datetime.utcnow().date()
        periods = _all_vacation_periods()
        current = [p for p in periods if p.start_date <= today <= p.end_date]
        upcoming = [p for p in periods if p.start_date > today]
        past = [p for p in periods if p.end_date < today]
        return render_template(
            "vacation.html",
            active_nav="vacation",
            current_periods=current,
            upcoming_periods=upcoming,
            past_periods=list(reversed(past)),
        )

    @app.route("/vacation/add", methods=["POST"])
    def vacation_add():
        payload = request.get_json(silent=True) or {}
        label = (payload.get("label") or "").strip() or "Trip"
        try:
            start_date = datetime.fromisoformat(payload.get("start_date")).date()
            end_date = datetime.fromisoformat(payload.get("end_date")).date()
        except (TypeError, ValueError):
            return jsonify(error="Enter valid start and end dates"), 400
        if end_date < start_date:
            return jsonify(error="End date can't be before the start date"), 400

        period = VacationPeriod(label=label, start_date=start_date, end_date=end_date)
        db.session.add(period)
        db.session.commit()
        return jsonify(period.to_dict()), 201

    @app.route("/vacation/<int:period_id>/delete", methods=["POST"])
    def vacation_delete(period_id):
        period = db.session.get(VacationPeriod, period_id)
        if period is None:
            abort(404)
        db.session.delete(period)
        db.session.commit()
        return jsonify(success=True)

    # -----------------------------------------------------------------
    # Dashboard
    # -----------------------------------------------------------------

    @app.route("/dashboard")
    def dashboard():
        profile = _get_profile()
        weigh_ins = _all_weigh_ins()
        latest_weight = weigh_ins[-1].weight_lbs if weigh_ins else None
        calorie_target = profile.calorie_target(latest_weight) if latest_weight else None

        entries = _todays_log_entries()
        consumed = round(sum(e.calories or 0 for e in entries)) if entries else 0

        today = datetime.utcnow().date()
        start = datetime.combine(today, datetime.min.time())
        end = start + timedelta(days=1)
        exercise_today = db.session.execute(
            db.select(ExerciseEntry)
            .filter(ExerciseEntry.logged_at >= start, ExerciseEntry.logged_at < end)
            .order_by(ExerciseEntry.logged_at)
        ).scalars().all()
        burned = round(sum(e.calories_burned for e in exercise_today))

        remaining = None
        if calorie_target is not None:
            remaining = calorie_target - consumed + burned

        return render_template(
            "dashboard.html",
            active_nav="dashboard",
            profile=profile,
            activity_levels=ACTIVITY_LEVELS,
            latest_weight=latest_weight,
            calorie_target=calorie_target,
            consumed=consumed,
            burned=burned,
            remaining=remaining,
            exercise_today=exercise_today,
        )

    @app.route("/dashboard/profile", methods=["POST"])
    def dashboard_profile_save():
        payload = request.get_json(silent=True) or {}
        profile = _get_profile()

        try:
            profile.height_in = float(payload.get("height_in")) if payload.get("height_in") else None
            profile.age = int(payload.get("age")) if payload.get("age") else None
        except (TypeError, ValueError):
            return jsonify(error="Height and age must be numbers"), 400

        sex = (payload.get("biological_sex") or "").strip().lower()
        if sex not in ("male", "female"):
            return jsonify(error="Select a biological sex -- Mifflin-St Jeor needs it"), 400
        profile.biological_sex = sex

        activity = (payload.get("activity_level") or "sedentary").strip()
        if activity not in ACTIVITY_LEVELS:
            return jsonify(error="Invalid activity level"), 400
        profile.activity_level = activity

        db.session.commit()
        return jsonify(profile.to_dict())

    @app.route("/exercise/add", methods=["POST"])
    def exercise_add():
        payload = request.get_json(silent=True) or {}
        activity = (payload.get("activity") or "").strip()
        if not activity:
            return jsonify(error="Name the activity"), 400
        try:
            calories_burned = float(payload.get("calories_burned"))
        except (TypeError, ValueError):
            return jsonify(error="Enter calories burned as a number"), 400
        if calories_burned < 0:
            return jsonify(error="Calories burned can't be negative"), 400

        entry = ExerciseEntry(activity=activity, calories_burned=calories_burned)
        db.session.add(entry)
        db.session.commit()
        return jsonify(entry.to_dict()), 201

    @app.route("/exercise/<int:entry_id>/delete", methods=["POST"])
    def exercise_delete(entry_id):
        entry = db.session.get(ExerciseEntry, entry_id)
        if entry is None:
            abort(404)
        db.session.delete(entry)
        db.session.commit()
        return jsonify(success=True)


app = create_app()

if __name__ == "__main__":
    app.run(debug=app.config.get("DEBUG", True), port=int(os.environ.get("PORT", 5000)))
