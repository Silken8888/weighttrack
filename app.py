import os
import re
import io
import json
import base64
import time
import uuid
import threading
from queue import Queue, Empty
from datetime import datetime, timedelta, date, timezone, time as dtime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, render_template, request, jsonify, abort, redirect, url_for
from sqlalchemy import inspect as sa_inspect, text as sa_text
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
    db, FoodItem, FoodLogEntry, FoodLogPhoto, FoodPhotoMemory, MEAL_TYPES,
    WeighIn, VacationPeriod, UserProfile, ExerciseEntry, ACTIVITY_LEVELS,
)


def _ensure_schema_up_to_date(app):
    """db.create_all() only creates TABLES that don't exist yet -- it
    never alters an existing table to add new columns. Confirmed live:
    food_log_entries already existed in production from an earlier
    deploy (meal photo logging), so when ai_protein_g/ai_carbs_g/
    ai_fat_g/batch_id were added to the model tonight, create_all()
    silently did nothing for them, and every query touching the table
    threw psycopg2.errors.UndefinedColumn.

    This is a minimal, dependency-free migration step appropriate for a
    personal app's scale (a real multi-developer project would use
    Alembic instead): compare each model's declared columns against
    what the live table actually has, and ALTER TABLE to add whatever's
    missing. Safe to run on every startup -- it's a no-op once the
    schema is caught up.
    """
    inspector = sa_inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    for model in (FoodItem, FoodLogEntry, FoodLogPhoto, FoodPhotoMemory, WeighIn, VacationPeriod, UserProfile, ExerciseEntry):
        table_name = model.__tablename__
        if table_name not in existing_tables:
            continue  # brand-new table -- create_all() already handled it
        existing_columns = {c["name"] for c in inspector.get_columns(table_name)}
        for column in model.__table__.columns:
            if column.name in existing_columns:
                continue
            col_type = column.type.compile(dialect=db.engine.dialect)
            with db.engine.begin() as conn:
                conn.execute(sa_text(f'ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type}'))
            print(f"INFO: schema sync -- added missing column {table_name}.{column.name}")


def create_app(config_name=None):
    config_name = config_name or os.environ.get("FLASK_ENV", "development")
    app = Flask(__name__)
    app.config.from_object(config_by_name[config_name])

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _ensure_schema_up_to_date(app)

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


def _extract_tool_input(data, tool_name):
    """Pull a forced tool call's input dict straight out of the response
    -- already a parsed object, no JSON-string parsing involved, since
    tool_choice guarantees the model responds with exactly this tool
    call in a structured content block rather than free-form text.
    """
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block.get("input")
    return None


def _user_timezone():
    """Reads the browser-detected IANA timezone from a cookie (set by
    JS on every page load -- see resolveTimezone() in main.js). Falls
    back to America/Los_Angeles, not UTC.

    Confirmed live, twice now, that the cookie can fail to arrive even
    when everything server-side is correct -- once from stale browser
    caching (fixed separately), and once in a fresh private-browsing
    session with no cache and no old cookies at all, which rules out
    caching as the cause there. Something about cookie writes from
    client-side JS just isn't reliable enough in every browsing context
    to be the *only* thing standing between this app and correct day
    boundaries. This is a single-user personal app, not a product
    serving people in different timezones -- defaulting to this
    specific user's actual, already-confirmed timezone (Central Coast,
    California, per the location stamp) whenever detection fails is a
    far smaller risk than defaulting to UTC, which is *never* correct
    for this user and is exactly what caused the original bug. The
    cookie still refines this when it works; it's no longer load-
    bearing for correctness.
    """
    tz_name = request.cookies.get("wt_tz")
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 -- any bad/unknown tz string falls back safely
            pass
    return ZoneInfo("America/Los_Angeles")


def _local_today(tz=None):
    return datetime.now(tz or _user_timezone()).date()


def _local_now_naive(tz=None):
    """'Right now', but as a naive datetime in the user's local time --
    used when stamping a new entry as happening right now, so it reads
    correctly against other local-date comparisons throughout the app
    (which all now work in local time, not UTC).
    """
    return datetime.now(tz or _user_timezone()).replace(tzinfo=None)


def _to_local_date(naive_utc_dt, tz=None):
    """Converts a stored datetime (naive, but representing UTC -- how
    logged_at is stored everywhere in this app) into the user's local
    calendar date. This is the piece that was missing everywhere:
    grouping/comparing by `.date()` directly on a UTC-stamped value
    gives the UTC calendar day, not the day it actually happened for
    the person.
    """
    tz = tz or _user_timezone()
    aware_utc = naive_utc_dt.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(tz).date()


def _local_day_bounds_utc(local_day, tz=None):
    """Given a local calendar date, returns (start, end) as naive
    datetimes representing that day's midnight-to-midnight span
    *converted to UTC* -- the correct range to query logged_at values
    (stored as naive UTC) against, rather than comparing UTC midnight
    boundaries directly.
    """
    tz = tz or _user_timezone()
    local_start = datetime.combine(local_day, dtime.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_start, utc_end


def _gather_app_context(app, tz=None):
    """One shared snapshot of real app data -- profile, latest weight,
    the app's own calorie-target calculation, recent food entries,
    recent exercise entries -- for any AI-driven feature to use.

    Centralized on purpose: confirmed repeatedly tonight that an AI
    feature only seeing its own narrow local slice of data produces
    genuinely wrong or confused behavior (recalculating a calorie
    target with no profile visible, not recognizing an exercise entry
    existed at all, an exercise estimate that can't tell it's about to
    duplicate something just logged). Every AI-driven feature should
    see the same real picture of the app, not maintain its own
    independent, driftable idea of what data is available.

    Returns a plain dict of primitives/strings only -- no ORM objects
    -- so it's safe to use outside the app_context() it was built
    inside without any detached-instance risk.
    """
    with app.app_context():
        recent_food = db.session.execute(
            db.select(FoodLogEntry).order_by(FoodLogEntry.logged_at.desc()).limit(20)
        ).scalars().all()
        recent_food_context = "\n".join(
            f'id={e.id}: "{e.display_name}", {e.meal_type}, logged '
            f'{e.logged_at.strftime("%Y-%m-%d %H:%M")}, '
            f'{round(e.calories) if e.calories is not None else "?"} cal'
            + (
                f', {len(e.photos)} photo(s) attached (photo ids: '
                f'{", ".join(str(p.id) for p in e.photos)})'
                if e.photos else ""
            )
            for e in recent_food
        ) or "(nothing logged yet)"

        recent_exercise = db.session.execute(
            db.select(ExerciseEntry).order_by(ExerciseEntry.logged_at.desc()).limit(20)
        ).scalars().all()
        recent_exercise_context = "\n".join(
            f'id={e.id}: "{e.activity}", logged '
            f'{e.logged_at.strftime("%Y-%m-%d %H:%M")}, '
            f'{round(e.calories_burned)} cal burned'
            for e in recent_exercise
        ) or "(nothing logged yet)"

        local_start, local_end = _local_day_bounds_utc(_local_today(tz), tz)
        today_entries = db.session.execute(
            db.select(FoodLogEntry).filter(
                FoodLogEntry.logged_at >= local_start, FoodLogEntry.logged_at < local_end
            )
        ).scalars().all()
        calories_today = round(sum(e.calories or 0 for e in today_entries))

        profile = db.session.get(UserProfile, 1)
        latest_weigh_in = db.session.execute(
            db.select(WeighIn).order_by(WeighIn.logged_at.desc()).limit(1)
        ).scalars().all()
        latest_weight = latest_weigh_in[0].weight_lbs if latest_weigh_in else None

        profile_lines = []
        if latest_weight is not None:
            profile_lines.append(f"current weight: {latest_weight} lbs")
        if profile:
            if profile.age:
                profile_lines.append(f"age: {profile.age}")
            if profile.biological_sex:
                profile_lines.append(f"biological sex: {profile.biological_sex}")
            if profile.height_in:
                profile_lines.append(f"height: {profile.height_in} in")
            if profile.activity_level:
                label = ACTIVITY_LEVELS.get(profile.activity_level, (profile.activity_level,))[0]
                profile_lines.append(f"activity level: {label}")
            if profile.goal_weight_lbs:
                profile_lines.append(f"goal weight: {profile.goal_weight_lbs} lbs")
        calorie_target = profile.calorie_target(latest_weight) if profile and latest_weight else None
        if calorie_target is not None:
            profile_lines.append(
                f"calorie target (already calculated by the app via "
                f"Mifflin-St Jeor, using the profile above): "
                f"{calorie_target} cal/day"
            )
        profile_lines.append(f"calories logged today so far: {calories_today}")
        profile_context = "\n".join(profile_lines) if profile_lines else "(no profile set up yet)"

    return {
        "latest_weight": latest_weight,
        "weight_kg": latest_weight * 0.453592 if latest_weight else None,
        "calorie_target": calorie_target,
        "profile_context": profile_context,
        "recent_food_context": recent_food_context,
        "recent_exercise_context": recent_exercise_context,
    }


def _estimate_meal_calories(app, image_bytes, ctx=None):
    """Ask Claude for a rough calorie estimate + short description of a
    meal photo. Returns (calories, description, error) -- calories/
    description are None on failure, error is None on success.

    Accepts the shared app-data context (recent food entries in
    particular) so the estimate can stay consistent with how similar
    meals were logged before, instead of estimating in a vacuum with
    no awareness of anything else in the app.
    """
    try:
        resized = _resize_for_ai(image_bytes)
    except UnidentifiedImageError:
        return None, None, "That doesn't look like a readable image."

    recent_food_context = (ctx or {}).get("recent_food_context", "(nothing logged yet)")

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
                    "plain description under 8 words. If this looks similar to "
                    "something already logged recently (see below), stay roughly "
                    "consistent with that estimate rather than estimating from "
                    "scratch.\n\n"
                    f"Recently logged food, for consistency:\n{recent_food_context}\n\n"
                    "Respond with ONLY a JSON "
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


def _normalize_food_name(name):
    """"Wheat Toast (2 slices)" and "Wheat Toast (3 slices)" should hit
    the same photo memory -- strip a trailing quantity in parens and
    normalize case/whitespace so they match.
    """
    if not name:
        return ""
    core = name.split(" (")[0]
    return " ".join(core.strip().lower().split())


def _auto_attach_remembered_photos(entry):
    """Called right after a new FoodLogEntry is created (and flushed, so
    it has an id) -- if this food name has photos remembered from a
    past entry, attach them automatically. Caller is responsible for
    the surrounding db.session.flush()/commit().
    """
    normalized = _normalize_food_name(entry.display_name)
    if not normalized:
        return
    remembered = db.session.execute(
        db.select(FoodPhotoMemory)
        .filter(FoodPhotoMemory.normalized_name == normalized)
        .order_by(FoodPhotoMemory.position)
    ).scalars().all()
    for i, mem in enumerate(remembered):
        db.session.add(FoodLogPhoto(food_log_entry_id=entry.id, url=mem.url, position=i))


def _remember_photo(name, url):
    """Called when a photo is manually attached via the picker -- saves
    it to the name-keyed memory so it's auto-applied to every future
    entry with a matching name, not just this one.
    """
    normalized = _normalize_food_name(name)
    if not normalized:
        return
    already = db.session.execute(
        db.select(FoodPhotoMemory).filter(
            FoodPhotoMemory.normalized_name == normalized, FoodPhotoMemory.url == url
        )
    ).scalar_one_or_none()
    if already:
        return  # already remembered, don't duplicate
    existing_count = db.session.execute(
        db.select(db.func.count()).select_from(FoodPhotoMemory).filter(
            FoodPhotoMemory.normalized_name == normalized
        )
    ).scalar()
    db.session.add(FoodPhotoMemory(normalized_name=normalized, url=url, position=existing_count))


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


def _to_local_datetime(naive_utc_dt, tz=None):
    tz = tz or _user_timezone()
    return naive_utc_dt.replace(tzinfo=timezone.utc).astimezone(tz)


def _local_to_utc_naive(local_date, local_time, tz=None):
    tz = tz or _user_timezone()
    local_dt = datetime.combine(local_date, local_time, tzinfo=tz)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _local_noon_utc(local_day, tz=None):
    """A UTC-naive timestamp representing local noon on the given local
    day -- used when logging a past-dated entry, anchored away from
    midnight so it doesn't drift to the wrong calendar day once
    reinterpreted back through _to_local_date(). Keeps storage
    consistently UTC-naive throughout the app (matching every existing
    datetime.utcnow() default) while still landing on the correct local
    day when read back.
    """
    tz = tz or _user_timezone()
    start_utc, _ = _local_day_bounds_utc(local_day, tz)
    return start_utc + timedelta(hours=12)


def _job_tz(job):
    """Background job workers have no Flask request context, so they
    can't read the timezone cookie directly -- the route handler that
    enqueued the job captures it into job["tz"] first (see each route
    below), and this turns that back into a ZoneInfo here. Same
    America/Los_Angeles fallback as _user_timezone(), for the same
    reason -- the cookie has proven unreliable enough that UTC is the
    wrong thing to fall back to for this specific user.
    """
    tz_name = job.get("tz")
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            pass
    return ZoneInfo("America/Los_Angeles")


def _run_exercise_estimate(app, job):
    """Estimate calories burned for a described activity ('half a mile
    walk'), personalized against the user's actual weight -- weight is
    a real, physiologically meaningful factor in exercise calorie burn
    (a heavier person burns more calories for the same walk), not a
    nicety.

    Confirmed live that asking Claude to produce the final calorie
    number directly (i.e. do "MET x weight_kg x duration_hours" as one
    mental-math step) under-shot the standard formula by roughly half
    for a user at higher body weight -- LLM arithmetic on real,
    non-round numbers isn't reliable even when the method is right.
    Fixed by splitting the task: Claude identifies the MET value and
    duration for the described activity (language understanding, which
    it's actually good at), and the app does the multiplication itself
    against the user's real weight, deterministically.

    Also parses the date from what they said ("yesterday I walked a
    mile") instead of always stamping it as right now -- same date
    handling the food agent already has, applied here too so exercise
    entries land on the correct day instead of always today.

    Uses the same shared app-data context every other AI feature uses
    -- in particular, recent exercise entries, so if the described
    activity looks like a near-duplicate of something already logged
    today or yesterday, the assistant can flag that in its note instead
    of silently stacking another entry on top (the exact failure mode
    that produced a 700+ calorie day from repeated retries earlier).
    """
    activity_text = job["activity"]
    tz = _job_tz(job)
    today = _local_today(tz).isoformat()

    ctx = _gather_app_context(app, tz)
    weight_kg = ctx["weight_kg"]

    if weight_kg is not None:
        instruction = (
            "Estimate an appropriate MET (metabolic equivalent) value for "
            "this activity, and the duration in hours -- infer duration "
            "from what's stated (a distance + typical pace, a stated "
            "time, or a reasonable default for the activity if neither "
            "is given). Don't compute calories yourself; the app will "
            "multiply MET x weight_kg x duration_hours using the real "
            "weight, so just return accurate met_value and "
            "duration_hours."
        )
        schema = (
            '{"met_value": <number>, "duration_hours": <number>, "date": '
            '"YYYY-MM-DD", "note": "<short note on any assumption you '
            'made, e.g. pace or duration, or empty string>"}'
        )
    else:
        # No weight on file at all -- can't do the real calculation, so
        # fall back to asking for calories directly rather than blocking
        # the feature entirely.
        instruction = (
            "No weight is on file, so give your single best rough "
            "calorie estimate directly for this activity using general "
            "averages."
        )
        schema = (
            '{"calories": <integer>, "date": "YYYY-MM-DD", "note": '
            '"<short note that this is a general estimate since no '
            'weight is on file, plus any other assumption>"}'
        )

    payload = {
        "model": app.config["ANTHROPIC_MEAL_MODEL"],
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": (
                f"Today's date is {today}. Estimate calories burned for a "
                "described exercise activity, for a personal fitness "
                f"tracker -- not medical advice. {instruction} Infer the "
                "date it happened from what they say (\"today\", "
                "\"yesterday\", unstated -> today) as an ISO date. If this "
                "looks like a near-duplicate of something already listed "
                "below (same activity, same day), say so briefly in the "
                "note so the user notices before it's logged twice.\n\n"
                f"Recently logged exercise, for duplicate-checking:\n"
                f"{ctx['recent_exercise_context']}\n\n"
                f'Activity described: "{activity_text}"\n\n'
                f"Respond with ONLY a JSON object, no markdown fences, no "
                f"other text: {schema}"
            ),
        }],
    }

    data, error = _call_claude(app, payload)
    if data is None:
        return None, error

    raw_text = _extract_claude_text(data)
    try:
        parsed = _parse_json_from_claude(raw_text)
    except json.JSONDecodeError:
        return None, "Got a response from the AI but couldn't parse it. Try rephrasing."

    note = (parsed.get("note") or "").strip() or None

    if weight_kg is not None:
        try:
            met_value = float(parsed["met_value"])
            duration_hours = float(parsed["duration_hours"])
        except (KeyError, TypeError, ValueError):
            return None, "Got a response from the AI but couldn't read it. Try rephrasing."
        calories = round(met_value * weight_kg * duration_hours)
    else:
        try:
            calories = int(parsed["calories"])
        except (KeyError, TypeError, ValueError):
            return None, "Got a response from the AI but couldn't read a calorie number from it."

    try:
        entry_date = datetime.fromisoformat(parsed.get("date", today)).date()
    except (ValueError, TypeError):
        entry_date = _local_today(tz)
    logged_at = (
        datetime.utcnow()
        if entry_date == _local_today(tz)
        else _local_noon_utc(entry_date, tz)
    )

    with app.app_context():
        entry = ExerciseEntry(activity=activity_text, calories_burned=calories, logged_at=logged_at)
        db.session.add(entry)
        db.session.commit()
        result = entry.to_dict()

    return {"entry": result, "note": note}, None


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

    ai_calories, ai_description, ai_error = _estimate_meal_calories(
        app, image_bytes, _gather_app_context(app, _job_tz(job))
    )
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
        db.session.flush()
        _auto_attach_remembered_photos(entry)
        db.session.commit()
        result = entry.to_dict()

    return {"entry": result, "note": ai_error}, None


def _run_food_agent(app, job):
    """The WeighTrack assistant: log new food described in plain
    language, adjust or delete something already logged if the message
    reads like a correction, or just answer a question -- not every
    message is a logging action.

    meal_type is optional now. When it's set (the inline "Tell The
    Assistant" panel, which still has the dropdown), it overrides
    whatever Claude would've guessed for every new item -- removes a
    whole category of ambiguity. When it's None (the general-purpose
    floating chat button), Claude infers it per item from context or
    time of day.

    Also sees the user's profile, latest weight, and the app's own
    calorie-target calculation -- not just recent food entries. Without
    this, a question like "recalculate my daily calorie intake" had
    nothing to work with even though the data genuinely exists
    elsewhere in the app (Dashboard, Weigh-In Log), which is exactly
    the kind of "the AI should be able to see this" gap worth fixing
    directly rather than explaining away.
    """
    message = job["message"]
    meal_type = job.get("meal_type")
    history = job.get("history") or []
    tz = _job_tz(job)
    today = _local_today(tz).isoformat()
    now_time = datetime.now(tz).strftime("%H:%M")

    ctx = _gather_app_context(app, tz)
    recent_context = ctx["recent_food_context"]
    exercise_context = ctx["recent_exercise_context"]
    profile_context = ctx["profile_context"]
    latest_weight = ctx["latest_weight"]

    if meal_type:
        meal_instruction = (
            f'The user already told the app this is for "{meal_type}" -- use '
            f'"{meal_type}" for every new item regardless of what they say.'
        )
    else:
        meal_instruction = (
            "No meal type was pre-selected. Infer it from what they say, or "
            "if it's not stated, from the current time (before 11am -> "
            "breakfast, 11am-3pm -> lunch, 3pm-5pm -> snack, after 5pm -> "
            "dinner)."
        )

    system_prompt = (
        f"Today's date is {today} (current time {now_time} UTC). "
        "You are the assistant inside WeighTrack, a personal "
        "nutrition tracker. You can: (1) log new food/drink the user "
        "describes, (2) log new exercise they describe (e.g. \"I "
        "walked a mile\"), (3) adjust an entry already logged (food or "
        "exercise) if they're correcting or changing something -- "
        "nutrition/calories burned (e.g. \"actually the toast was 3 "
        "slices\", \"that walk was more like 300 calories\") or WHEN "
        "it was logged (e.g. \"that was actually at 7am\", \"change "
        "the time to 6:15pm yesterday for my exercise\") -- adjusting "
        "the logged time/date is fully supported for BOTH food and "
        "exercise entries, don't say you can't do it or that you don't "
        "see an entry without checking the exercise list below first, "
        "(4) delete an entry (food or exercise) they ask to remove, "
        "(5) remove a specific photo attached to a food entry -- "
        "photos ARE supported (each entry below shows its attached "
        "photo ids if it has any) -- don't say photos aren't stored, "
        "that's wrong. You can't actually see what a photo looks like "
        "from just its id/url though, so if they describe one by "
        "appearance (e.g. \"the black one\") and there's more than one "
        "photo on that entry, ask which position it is rather than "
        "guessing -- but if there's only one photo on the entry they "
        "mean, just remove it, or (6) just answer a question -- not "
        f"everything is a logging action.\n\n{meal_instruction}\n\n"
        f"The user's profile and current stats -- if they ask about their "
        f"calorie target, use the already-calculated number below rather "
        f"than re-deriving your own estimate (it needs to match what the "
        f"Dashboard shows); if something's missing that you'd need, say "
        f"what's missing rather than guessing:\n{profile_context}\n\n"
        f"Recently logged FOOD entries, for reference if they're "
        f"adjusting or deleting something (refer to one by its id; "
        f"logged_at is shown so you know what to correct it from):\n"
        f"{recent_context}\n\n"
        f"Recently logged EXERCISE entries, same idea -- check this "
        f"list before saying you don't see an exercise entry:\n"
        f"{exercise_context}\n\n"
        "Break new food into distinct items the same way you always "
        "do (e.g. toast and peanut butter are separate items, since "
        "they have very different nutrition profiles). For each new "
        "food item, give your single best rough estimate of calories, "
        "protein (g), carbs (g), and fat (g) for the stated "
        "quantity -- these are estimates for personal tracking, not "
        "medical or nutritional advice. For new exercise, don't "
        "compute calories yourself -- identify an appropriate MET "
        "(metabolic equivalent) value and the duration in hours (infer "
        "from a stated distance + typical pace, a stated time, or a "
        "reasonable default), and the app will multiply MET x weight "
        "x duration using the real logged weight, since that's more "
        "reliable than doing that arithmetic yourself. Infer the date "
        "from context (\"today\", \"yesterday\", a specific date, or "
        "unstated -> today) as an ISO date (YYYY-MM-DD) for anything "
        "new or adjusted.\n\n"
        "For adjustments and deletions, always include which kind of "
        "entry it is (\"food\" or \"exercise\") along with the id, "
        "since the two lists above have independent ids.\n\n"
        "Always call the log_and_reply tool to respond, every turn -- "
        "even for a pure question, call it with empty arrays and put "
        "your answer in reply."
    )

    # Real multi-turn memory: prior turns in this conversation (floating
    # chat only -- the inline dropdown form is single-shot by design) get
    # replayed as actual message history, not just the current message in
    # isolation. Only the natural-language reply from past assistant
    # turns is replayed, not the raw tool-call envelope -- Claude doesn't
    # need to see its own past tool call to follow the thread, just what
    # it said.
    messages = []
    for turn in history:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:4000]})
    messages.append({"role": "user", "content": message})

    # Tool-use instead of asking for a plain-text JSON blob: confirmed
    # live that over a longer conversation, the model can drift into
    # wrapping the JSON with conversational prose (e.g. after several
    # natural back-and-forth turns), which broke text-based parsing --
    # "Got a response from the AI but couldn't parse it." Forcing a tool
    # call makes the structure a guarantee from the API itself rather
    # than an instruction the model has to keep remembering to follow.
    nullable_number = {"type": ["number", "null"]}
    tool = {
        "name": "log_and_reply",
        "description": (
            "Log new food or exercise, adjust or delete existing entries "
            "of either kind, and reply to the user. Call this every turn, "
            "even for a pure question -- use empty arrays when nothing "
            "needs to change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "New food/drink items to log.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "string"},
                            "meal_type": {"type": "string", "enum": list(MEAL_TYPES)},
                            "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                            "calories": {"type": "integer"},
                            "protein_g": {"type": "number"},
                            "carbs_g": {"type": "number"},
                            "fat_g": {"type": "number"},
                        },
                        "required": ["name", "calories"],
                    },
                },
                "exercise_items": {
                    "type": "array",
                    "description": "New exercise to log. Give met_value and duration_hours, not calories -- the app computes calories itself from the real logged weight.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "activity": {"type": "string"},
                            "met_value": {"type": "number"},
                            "duration_hours": {"type": "number"},
                            "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        },
                        "required": ["activity", "met_value", "duration_hours"],
                    },
                },
                "adjustments": {
                    "type": "array",
                    "description": "Corrections to entries already logged, referenced by entity + id.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity": {"type": "string", "enum": ["food", "exercise"]},
                            "id": {"type": "integer"},
                            "calories": nullable_number,
                            "protein_g": nullable_number,
                            "carbs_g": nullable_number,
                            "fat_g": nullable_number,
                            "description": {"type": ["string", "null"], "description": "New name/description (food) or new activity text (exercise), or null if unchanged."},
                            "date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD or null if unchanged"},
                            "time": {"type": ["string", "null"], "description": "HH:MM 24-hour or null if unchanged"},
                        },
                        "required": ["entity", "id"],
                    },
                },
                "deletions": {
                    "type": "array",
                    "description": "Entries to delete, referenced by entity + id.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity": {"type": "string", "enum": ["food", "exercise"]},
                            "id": {"type": "integer"},
                        },
                        "required": ["entity", "id"],
                    },
                },
                "photo_deletions": {
                    "type": "array",
                    "description": "Specific attached photos to remove from a food entry, referenced by their photo id (shown in the recent entries list when an entry has photos).",
                    "items": {"type": "integer"},
                },
                "reply": {
                    "type": "string",
                    "description": "Short natural response -- confirm what you did and the total calories, or the answer if it wasn't a logging action.",
                },
            },
            "required": ["items", "exercise_items", "adjustments", "deletions", "photo_deletions", "reply"],
        },
    }

    payload = {
        "model": app.config["ANTHROPIC_AGENT_MODEL"],
        "max_tokens": 1500,
        "system": system_prompt,
        "messages": messages,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": "log_and_reply"},
    }

    data, error = _call_claude(app, payload, timeout_multiplier=3)
    if data is None:
        return None, error

    parsed = _extract_tool_input(data, "log_and_reply")
    if parsed is None:
        return None, "Got a response from the AI but couldn't read it. Try rephrasing."

    items = parsed.get("items") or []
    exercise_items = parsed.get("exercise_items") or []
    adjustments = parsed.get("adjustments") or []
    deletions = parsed.get("deletions") or []
    photo_deletions = parsed.get("photo_deletions") or []
    reply = (parsed.get("reply") or "").strip() or "Done."
    batch_id = uuid.uuid4().hex if items else None
    weight_kg = latest_weight * 0.453592 if latest_weight else None

    def _num(source, key):
        val = source.get(key)
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    created, created_exercise, adjusted, deleted_ids = [], [], [], []
    with app.app_context():
        for item in items:
            try:
                try:
                    item_date = datetime.fromisoformat(item.get("date", today)).date()
                except (ValueError, TypeError):
                    item_date = _local_today(tz)

                logged_at = (
                    datetime.utcnow()
                    if item_date == _local_today(tz)
                    else _local_noon_utc(item_date, tz)
                )

                item_meal_type = meal_type or (item.get("meal_type") or "snack").strip().lower()
                if item_meal_type not in MEAL_TYPES:
                    item_meal_type = "snack"

                name = (item.get("name") or "Food item").strip()[:200]
                quantity = (item.get("quantity") or "").strip()
                description = f"{name} ({quantity})" if quantity else name

                entry = FoodLogEntry(
                    food_item_id=None,
                    description=description[:200],
                    meal_type=item_meal_type,
                    servings=1.0,
                    logged_at=logged_at,
                    ai_calories=_num(item, "calories"),
                    ai_protein_g=_num(item, "protein_g"),
                    ai_carbs_g=_num(item, "carbs_g"),
                    ai_fat_g=_num(item, "fat_g"),
                    batch_id=batch_id,
                )
                db.session.add(entry)
                db.session.flush()
                _auto_attach_remembered_photos(entry)
                created.append(entry)
            except Exception:  # noqa: BLE001 -- one malformed item shouldn't drop the rest
                continue

        for ex_item in exercise_items:
            try:
                try:
                    ex_date = datetime.fromisoformat(ex_item.get("date", today)).date()
                except (ValueError, TypeError):
                    ex_date = _local_today(tz)
                ex_logged_at = (
                    datetime.utcnow()
                    if ex_date == _local_today(tz)
                    else _local_noon_utc(ex_date, tz)
                )

                activity = (ex_item.get("activity") or "Exercise").strip()[:200]
                if weight_kg is not None:
                    met_value = float(ex_item.get("met_value"))
                    duration_hours = float(ex_item.get("duration_hours"))
                    calories_burned = round(met_value * weight_kg * duration_hours)
                else:
                    # No weight on file -- can't do the real calculation,
                    # fall back to whatever Claude estimated as a rough
                    # calorie figure if it gave one, else skip the item
                    # rather than logging a meaningless 0.
                    fallback = _num(ex_item, "calories")
                    if fallback is None:
                        continue
                    calories_burned = round(fallback)

                exercise_entry = ExerciseEntry(
                    activity=activity, calories_burned=calories_burned, logged_at=ex_logged_at
                )
                db.session.add(exercise_entry)
                created_exercise.append(exercise_entry)
            except Exception:  # noqa: BLE001
                continue

        for adj in adjustments:
            try:
                entity = adj.get("entity")
                if entity == "exercise":
                    ex_entry = db.session.get(ExerciseEntry, int(adj.get("id")))
                    if ex_entry is None:
                        continue
                    if adj.get("calories") is not None:
                        ex_entry.calories_burned = _num(adj, "calories")
                    if adj.get("description"):
                        ex_entry.activity = str(adj["description"]).strip()[:200]

                    new_date_str = adj.get("date")
                    new_time_str = adj.get("time")
                    if new_date_str or new_time_str:
                        try:
                            existing_local = _to_local_datetime(ex_entry.logged_at, tz)
                            target_date = (
                                datetime.fromisoformat(new_date_str).date()
                                if new_date_str else existing_local.date()
                            )
                            if new_time_str:
                                hour, minute = (int(p) for p in new_time_str.split(":")[:2])
                                target_time = existing_local.time().replace(hour=hour, minute=minute)
                            else:
                                target_time = existing_local.time()
                            ex_entry.logged_at = _local_to_utc_naive(target_date, target_time, tz)
                        except (ValueError, TypeError, IndexError):
                            pass
                    adjusted.append(ex_entry)
                    continue

                entry = db.session.get(FoodLogEntry, int(adj.get("id")))
                if entry is None:
                    continue
                if adj.get("calories") is not None:
                    entry.manual_calories = _num(adj, "calories")
                if adj.get("protein_g") is not None:
                    entry.ai_protein_g = _num(adj, "protein_g")
                if adj.get("carbs_g") is not None:
                    entry.ai_carbs_g = _num(adj, "carbs_g")
                if adj.get("fat_g") is not None:
                    entry.ai_fat_g = _num(adj, "fat_g")
                if adj.get("description"):
                    entry.description = str(adj["description"]).strip()[:200]

                # Correct WHEN it was logged, not just what it is -- keep
                # whichever of date/time wasn't specified as-is on the
                # existing logged_at, only overwrite the part that changed.
                new_date_str = adj.get("date")
                new_time_str = adj.get("time")
                if new_date_str or new_time_str:
                    try:
                        existing_local = _to_local_datetime(entry.logged_at, tz)
                        target_date = (
                            datetime.fromisoformat(new_date_str).date()
                            if new_date_str else existing_local.date()
                        )
                        if new_time_str:
                            hour, minute = (int(p) for p in new_time_str.split(":")[:2])
                            target_time = existing_local.time().replace(hour=hour, minute=minute)
                        else:
                            target_time = existing_local.time()
                        entry.logged_at = _local_to_utc_naive(target_date, target_time, tz)
                    except (ValueError, TypeError, IndexError):
                        pass  # malformed date/time from the AI -- skip the time change, keep the rest

                adjusted.append(entry)
            except (TypeError, ValueError):
                continue

        for del_item in deletions:
            try:
                if isinstance(del_item, dict):
                    del_entity = del_item.get("entity")
                    del_id = int(del_item.get("id"))
                else:
                    # Tolerate a bare id (older shape / model slip) as a food deletion
                    del_entity, del_id = "food", int(del_item)

                if del_entity == "exercise":
                    ex_entry = db.session.get(ExerciseEntry, del_id)
                    if ex_entry is not None:
                        deleted_ids.append(del_id)
                        db.session.delete(ex_entry)
                    continue

                entry = db.session.get(FoodLogEntry, del_id)
                if entry is not None:
                    deleted_ids.append(entry.id)
                    db.session.delete(entry)
            except (TypeError, ValueError):
                continue

        deleted_photo_ids = []
        for photo_id in photo_deletions:
            try:
                photo = db.session.get(FoodLogPhoto, int(photo_id))
                if photo is not None:
                    deleted_photo_ids.append(photo.id)
                    db.session.delete(photo)
            except (TypeError, ValueError):
                continue

        db.session.commit()
        entries = [e.to_dict() for e in created] + [e.to_dict() for e in created_exercise]
        adjusted_dicts = [e.to_dict() for e in adjusted]

    return {
        "reply": reply,
        "entries": entries,
        "adjusted": adjusted_dicts,
        "deleted": deleted_ids,
        "deleted_photos": deleted_photo_ids,
    }, None


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
            elif job["kind"] == "exercise_estimate":
                outcome, error = _run_exercise_estimate(app, job)
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

def _compute_streak(weigh_ins, vacation_periods, tz=None):
    """Consecutive calendar days with a weigh-in, walking backward from
    today, treating vacation-covered days as grace days that count
    toward the streak without needing an entry -- per the original
    spec, a trip shouldn't zero out (or silently discount) an
    established streak. Today itself is allowed to be pending (not yet
    logged) without breaking anything, since the day isn't over.

    "Today" and each entry's day are both computed in the user's local
    timezone -- comparing local calendar days consistently, not the
    UTC day a timestamp happens to fall on.
    """
    if not weigh_ins:
        return 0
    tz = tz or _user_timezone()
    logged_dates = {_to_local_date(w.logged_at, tz) for w in weigh_ins}

    def on_vacation(d):
        return any(vp.start_date <= d <= vp.end_date for vp in vacation_periods)

    streak = 0
    day = _local_today(tz)
    if day not in logged_dates and not on_vacation(day):
        day -= timedelta(days=1)
    while day in logged_dates or on_vacation(day):
        streak += 1
        day -= timedelta(days=1)
    return streak


def _rolling_average(weigh_ins, as_of, window_days=7, tz=None):
    tz = tz or _user_timezone()
    window_start = as_of - timedelta(days=window_days - 1)
    values = [w.weight_lbs for w in weigh_ins if window_start <= _to_local_date(w.logged_at, tz) <= as_of]
    if not values:
        return None
    return sum(values) / len(values)


def _calculate_bmi(weight_lbs, height_in):
    """Standard imperial BMI formula: 703 * lbs / inches^2."""
    if not weight_lbs or not height_in:
        return None
    return round(703 * weight_lbs / (height_in ** 2), 1)


def _bmi_color(bmi):
    """Collapses the standard clinical BMI categories (normal/
    underweight, overweight, obese) into the three colors asked for:
    green = normal range, yellow = overweight ("moderately over"),
    red = obese ("severely over"). Underweight is folded into green
    here since the ask was specifically about being over, not under.
    """
    if bmi is None:
        return None
    if bmi < 25:
        return "green"
    if bmi < 30:
        return "yellow"
    return "red"


def _weigh_in_chart_data(weigh_ins, tz=None):
    """One point per actual logged weigh-in (not synthesized empty
    days -- per the direct request, this tracks each real day, not a
    smoothed average), each carrying that day's calories consumed and
    burned for the hover tooltip. Each day's boundary is computed in
    the user's local timezone, not UTC, so a meal logged at 9pm doesn't
    get attributed to the wrong calendar day.
    """
    tz = tz or _user_timezone()
    data = []
    for w in weigh_ins:
        day = _to_local_date(w.logged_at, tz)
        start, end = _local_day_bounds_utc(day, tz)

        day_food = db.session.execute(
            db.select(FoodLogEntry).filter(FoodLogEntry.logged_at >= start, FoodLogEntry.logged_at < end)
        ).scalars().all()
        consumed = round(sum(e.calories or 0 for e in day_food))

        day_exercise = db.session.execute(
            db.select(ExerciseEntry).filter(ExerciseEntry.logged_at >= start, ExerciseEntry.logged_at < end)
        ).scalars().all()
        burned = round(sum(e.calories_burned or 0 for e in day_exercise))

        data.append({
            "date": day.isoformat(),
            "label": day.strftime("%b %-d"),
            "weight": w.weight_lbs,
            "consumed": consumed,
            "burned": burned,
        })
    return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register_routes(app):

    @app.context_processor
    def inject_asset_version():
        """Cache-busting for static assets, based on each file's real
        modification time. Confirmed as a real, live risk: this app's
        JS/CSS changed on nearly every redeploy tonight, but nothing
        forced browsers to fetch the new copies -- a browser could keep
        serving a stale cached main.js indefinitely, meaning a shipped
        fix silently never actually runs until the person happens to
        hard-refresh. Recomputed fresh on every request rather than
        cached at startup, so it's correct even if a file changes
        without a full app restart.
        """
        def asset_version(rel_path):
            try:
                full_path = os.path.join(app.static_folder, rel_path)
                return str(int(os.path.getmtime(full_path)))
            except OSError:
                return "1"
        return {"asset_version": asset_version}

    @app.template_filter("local_time")
    def local_time_filter(dt, fmt="%-I:%M %p"):
        """Displays a stored (UTC-naive) datetime in the user's actual
        local time, not the raw UTC clock time it's stored as -- without
        this, timeline/history timestamps show the wrong time-of-day for
        anyone not in UTC, on top of the day-bucketing issue this whole
        fix addresses.
        """
        if dt is None:
            return ""
        return _to_local_datetime(dt).strftime(fmt)

    @app.template_filter("local_date")
    def local_date_filter(dt, fmt="%a, %b %-d"):
        if dt is None:
            return ""
        return _to_local_datetime(dt).strftime(fmt)

    def _all_food_items():
        return db.session.execute(
            db.select(FoodItem).order_by(FoodItem.nickname)
        ).scalars().all()

    def _todays_log_entries():
        start, end = _local_day_bounds_utc(_local_today())
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

    @app.route("/")
    def index():
        return dashboard()

    @app.route("/food")
    def food_library():
        """URL and endpoint name unchanged (so every url_for('food_library')
        reference elsewhere keeps working), but this is now the "Log" tab
        -- every input field in the app lives here (food, exercise,
        weigh-in, profile settings) and nowhere else. All of it used to
        be scattered across three separate pages, several of which mixed
        inputs with the metrics they fed -- entering a weight and seeing
        the goal-progress number change on the same screen, for example
        -- which made the actual flow hard to follow. Every *result* of
        these inputs now lives on the Dashboard instead.
        """
        profile = _get_profile()
        return render_template(
            "food_library.html",
            active_nav="log",
            profile=profile,
            activity_levels=ACTIVITY_LEVELS,
        )

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
        db.session.flush()
        _auto_attach_remembered_photos(entry)
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
        """The WeighTrack assistant. Two entry points share this same
        route: the inline "Tell The Assistant" panel (has a meal-type
        dropdown -- always sends meal_type) and the general-purpose
        floating chat button (no dropdown -- meal_type is inferred by
        Claude from context or time of day when omitted). Either way it
        can log new food, adjust or delete something already logged, or
        just answer a question.
        """
        payload = request.get_json(silent=True) or {}
        message = (payload.get("message") or "").strip()
        if not message:
            return jsonify(error="Say something first"), 400
        if len(message) > 2000:
            return jsonify(error="That's a lot -- try breaking it into a shorter message"), 400

        raw_meal_type = (payload.get("meal_type") or "").strip().lower()
        if raw_meal_type and raw_meal_type not in MEAL_TYPES:
            return jsonify(error="That's not a valid meal type"), 400
        meal_type = raw_meal_type or None

        # Conversation history from the floating chat -- capped at the
        # last 20 turns so a very long session doesn't blow up the
        # prompt size. Each entry is trusted only for role/content;
        # anything else the client sends is ignored.
        raw_history = payload.get("history") or []
        history = []
        if isinstance(raw_history, list):
            for turn in raw_history[-20:]:
                if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
                    history.append({
                        "role": turn["role"],
                        "content": str(turn.get("content") or "")[:4000],
                    })

        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _jobs[job_id] = {
                "kind": "agent_message",
                "status": "pending",
                "message": message,
                "meal_type": meal_type,
                "history": history,
                "tz": request.cookies.get("wt_tz"),
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
            db.session.flush()
            _auto_attach_remembered_photos(entry)
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
                "tz": request.cookies.get("wt_tz"),
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

        raw_calories = payload.get("calories")
        if raw_calories not in (None, ""):
            try:
                calories = float(raw_calories)
            except (TypeError, ValueError):
                return jsonify(error="Enter a valid number of calories"), 400
            if calories < 0:
                return jsonify(error="Calories can't be negative"), 400
            entry.manual_calories = calories

        # Same date/time correction pattern already used by the AI
        # assistant's adjustments -- keep whichever of date/time wasn't
        # given as-is, only change the part that was actually provided.
        raw_date = (payload.get("date") or "").strip()
        raw_time = (payload.get("time") or "").strip()
        if raw_date or raw_time:
            try:
                tz = _user_timezone()
                current_local = _to_local_datetime(entry.logged_at, tz)
                target_date = datetime.fromisoformat(raw_date).date() if raw_date else current_local.date()
                if raw_time:
                    hour, minute = (int(p) for p in raw_time.split(":")[:2])
                    target_time = current_local.time().replace(hour=hour, minute=minute)
                else:
                    target_time = current_local.time()
                entry.logged_at = _local_to_utc_naive(target_date, target_time, tz)
            except (ValueError, TypeError, IndexError):
                return jsonify(error="That date or time doesn't look right"), 400

        db.session.commit()
        return jsonify(entry.to_dict())

    @app.route("/log/<int:entry_id>/photos/add", methods=["POST"])
    def log_photo_attach(entry_id):
        """Manually attach a photo by URL to a log entry -- like Plex's
        'paste a URL' poster picker. Several can be attached to the same
        entry; they render as a thumbnail row. No fetching or validation
        of the URL server-side (that would mean a network call in a
        request handler, which this app avoids everywhere else too) --
        if the URL is bad, the <img> tag just fails to load client-side.
        """
        entry = db.session.get(FoodLogEntry, entry_id)
        if entry is None:
            abort(404)

        payload = request.get_json(silent=True) or {}
        url = (payload.get("url") or "").strip()
        if not url:
            return jsonify(error="Paste a URL first"), 400
        if not (url.startswith("http://") or url.startswith("https://")):
            return jsonify(error="That doesn't look like a URL"), 400
        if len(url) > 1000:
            return jsonify(error="That URL is too long"), 400

        next_position = len(entry.photos)
        photo = FoodLogPhoto(food_log_entry_id=entry.id, url=url, position=next_position)
        db.session.add(photo)
        _remember_photo(entry.display_name, url)
        db.session.commit()
        return jsonify(photo.to_dict()), 201

    @app.route("/log/<int:entry_id>/photos/<int:photo_id>/delete", methods=["POST"])
    def log_photo_remove(entry_id, photo_id):
        photo = db.session.get(FoodLogPhoto, photo_id)
        if photo is None or photo.food_log_entry_id != entry_id:
            abort(404)
        db.session.delete(photo)
        db.session.commit()
        return jsonify(success=True)

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
            if "adjusted" in job:
                response["adjusted"] = job["adjusted"]
            if "deleted" in job:
                response["deleted"] = job["deleted"]
            if "deleted_photos" in job:
                response["deleted_photos"] = job["deleted_photos"]
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
        """Endpoint name kept alive for any existing url_for() references
        -- the actual content (chart, milestones, streak) now lives on
        the Dashboard, and the entry list + logging form live on the Log
        tab, so this just sends anyone who lands here to the right place.
        """
        return redirect(url_for("dashboard"))


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
        # use local noon on that date (we only have a date, not a time,
        # from the form). Milestones and the "first entry" reference
        # already work off whichever logged_at is earliest, sorted at
        # query time -- not insertion order -- so backdating an earlier
        # entry automatically recalibrates "day one" without any extra
        # logic.
        raw_date = (payload.get("date") or "").strip()
        logged_at = _local_now_naive()
        if raw_date:
            try:
                entry_date = datetime.fromisoformat(raw_date).date()
                today_local = _local_today()
                if entry_date != today_local:
                    logged_at = _local_noon_utc(entry_date)
                if entry_date > today_local:
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
        today = _local_today()
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
        vacations = _all_vacation_periods()
        latest_weight = weigh_ins[-1].weight_lbs if weigh_ins else None
        starting_weight = weigh_ins[0].weight_lbs if weigh_ins else None
        calorie_target = profile.calorie_target(latest_weight) if latest_weight else None
        streak = _compute_streak(weigh_ins, vacations)
        rolling_avg = _rolling_average(weigh_ins, _local_today()) if weigh_ins else None
        chart_data = _weigh_in_chart_data(weigh_ins)

        pounds_lost = None
        if starting_weight is not None and latest_weight is not None:
            pounds_lost = max(0, round(starting_weight - latest_weight, 1))

        goal_weight = profile.goal_weight_lbs
        lbs_to_goal = None
        if goal_weight is not None and latest_weight is not None:
            lbs_to_goal = round(max(0, latest_weight - goal_weight), 1)

        start_date = profile.program_start_date or date(2026, 6, 28)
        days_since_start = max(0, (_local_today() - start_date).days)

        bmi = _calculate_bmi(latest_weight, profile.height_in)
        bmi_color = _bmi_color(bmi)

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

        entries = _todays_log_entries()
        consumed = round(sum(e.calories or 0 for e in entries)) if entries else 0

        # Grouped by day (last 7 days), bold-headered per day -- directly
        # requested after entries from different days visually ran
        # together with no separation. Also means any future day-
        # boundary misattribution would show up immediately under the
        # wrong header instead of blending in silently, the way this
        # bug did before it was caught.
        today = _local_today()
        food_history_start, _ = _local_day_bounds_utc(today - timedelta(days=7))
        recent_food = db.session.execute(
            db.select(FoodLogEntry)
            .filter(FoodLogEntry.logged_at >= food_history_start)
            .order_by(FoodLogEntry.logged_at.desc())
        ).scalars().all()

        food_by_day = {}
        food_day_order = []
        for e in recent_food:
            day = _to_local_date(e.logged_at)
            if day not in food_by_day:
                food_by_day[day] = []
                food_day_order.append(day)
            food_by_day[day].append(e)

        timeline_history = [
            {
                "date": day,
                "label": f'Today \u00b7 {day.strftime("%a, %b %-d")}' if day == today else day.strftime("%A, %b %-d"),
                "entries": list(reversed(food_by_day[day])),
                "total": round(sum(e.calories or 0 for e in food_by_day[day])),
            }
            for day in food_day_order
        ]

        start, end = _local_day_bounds_utc(today)
        exercise_today = db.session.execute(
            db.select(ExerciseEntry)
            .filter(ExerciseEntry.logged_at >= start, ExerciseEntry.logged_at < end)
            .order_by(ExerciseEntry.logged_at)
        ).scalars().all()
        burned = round(sum(e.calories_burned for e in exercise_today))

        remaining = None
        if calorie_target is not None:
            remaining = calorie_target - consumed + burned

        # Exercise history, grouped by day -- "Today's Exercise" only ever
        # showed today, so a past-dated entry (e.g. "yesterday I walked a
        # mile") was invisible anywhere in the UI even though it existed
        # in the database. This surfaces the last 30 days, letting past
        # entries actually be seen (and cleaned up if something got
        # logged more than once).
        history_start, _ = _local_day_bounds_utc(today - timedelta(days=30))
        recent_exercise = db.session.execute(
            db.select(ExerciseEntry)
            .filter(ExerciseEntry.logged_at >= history_start)
            .order_by(ExerciseEntry.logged_at.desc())
        ).scalars().all()

        exercise_by_day = {}
        day_order = []
        for e in recent_exercise:
            day = _to_local_date(e.logged_at)
            if day not in exercise_by_day:
                exercise_by_day[day] = []
                day_order.append(day)
            exercise_by_day[day].append(e)

        exercise_history = [
            {
                "date": day,
                "label": f'Today \u00b7 {day.strftime("%a, %b %-d")}' if day == today else day.strftime("%a, %b %-d"),
                "entries": list(reversed(exercise_by_day[day])),
                "total": round(sum(e.calories_burned for e in exercise_by_day[day])),
            }
            for day in day_order
        ]

        return render_template(
            "dashboard.html",
            active_nav="dashboard",
            profile=profile,
            weigh_ins=list(reversed(weigh_ins)),
            log_entries=entries,
            timeline_history=timeline_history,
            calories_today=round(consumed) if entries else None,
            latest_weight=latest_weight,
            pounds_lost=pounds_lost,
            goal_weight=goal_weight,
            lbs_to_goal=lbs_to_goal,
            days_since_start=days_since_start,
            streak=streak,
            rolling_avg=round(rolling_avg, 1) if rolling_avg is not None else None,
            chart_data=chart_data,
            milestones=milestones,
            calorie_target=calorie_target,
            consumed=consumed,
            burned=burned,
            remaining=remaining,
            exercise_today=exercise_today,
            exercise_history=exercise_history,
            bmi=bmi,
            bmi_color=bmi_color,
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

        raw_goal = payload.get("goal_weight_lbs")
        if raw_goal not in (None, ""):
            try:
                profile.goal_weight_lbs = float(raw_goal)
            except (TypeError, ValueError):
                return jsonify(error="Goal weight must be a number"), 400
        else:
            profile.goal_weight_lbs = None

        raw_start = (payload.get("program_start_date") or "").strip()
        if raw_start:
            try:
                profile.program_start_date = datetime.fromisoformat(raw_start).date()
            except ValueError:
                return jsonify(error="That start date doesn't look right"), 400

        db.session.commit()
        return jsonify(profile.to_dict())

    @app.route("/exercise/add", methods=["POST"])
    def exercise_add():
        """Describe the activity, Claude estimates calories burned using
        your actual weight/age/sex/activity level -- not a generic
        lookup table. Same background-job pattern as everything else
        that calls an external API.
        """
        payload = request.get_json(silent=True) or {}
        activity = (payload.get("activity") or "").strip()
        if not activity:
            return jsonify(error="Describe the activity first"), 400
        if len(activity) > 500:
            return jsonify(error="That's a lot -- try a shorter description"), 400

        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _jobs[job_id] = {
                "kind": "exercise_estimate",
                "status": "pending",
                "activity": activity,
                "tz": request.cookies.get("wt_tz"),
                "created_at": datetime.utcnow(),
                "results": None,
                "error": None,
            }
        _search_queue.put(job_id)

        return jsonify(job_id=job_id), 202

    @app.route("/exercise/<int:entry_id>/delete", methods=["POST"])
    def exercise_delete(entry_id):
        entry = db.session.get(ExerciseEntry, entry_id)
        if entry is None:
            abort(404)
        db.session.delete(entry)
        db.session.commit()
        return jsonify(success=True)

    @app.route("/exercise/delete-day", methods=["POST"])
    def exercise_delete_day():
        """Clear every exercise entry on a given day in one action --
        faster than deleting duplicates one at a time when several
        stacked up on the same day.
        """
        payload = request.get_json(silent=True) or {}
        raw_date = (payload.get("date") or "").strip()
        try:
            day = datetime.fromisoformat(raw_date).date()
        except ValueError:
            return jsonify(error="Invalid date"), 400

        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        entries = db.session.execute(
            db.select(ExerciseEntry).filter(
                ExerciseEntry.logged_at >= start, ExerciseEntry.logged_at < end
            )
        ).scalars().all()
        count = len(entries)
        for e in entries:
            db.session.delete(e)
        db.session.commit()
        return jsonify(deleted=count)


app = create_app()

if __name__ == "__main__":
    app.run(debug=app.config.get("DEBUG", True), port=int(os.environ.get("PORT", 5000)))
