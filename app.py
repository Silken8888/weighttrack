import os
import re
import io
import time
import uuid
import threading
from queue import Queue, Empty
from datetime import datetime, timedelta

import requests
from flask import Flask, render_template, request, jsonify, abort
from PIL import Image, UnidentifiedImageError
from pyzbar.pyzbar import decode as decode_barcodes
import pytesseract

from config import config_by_name
from models import db, FoodItem, FoodLogEntry, MEAL_TYPES


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


def _fetch_from_open_food_facts(app, query, page_size):
    """One search attempt against Open Food Facts, with one retry on the
    transient 502/503s seen during testing (confirmed to clear within
    seconds). Always sends a real User-Agent -- Open Food Facts throttles
    or rejects requests without one.
    """
    headers = {"User-Agent": app.config["OFF_USER_AGENT"]}
    params = {
        "search_terms": query,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": page_size,
    }

    attempts = app.config["OFF_RETRY_COUNT"] + 1
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
                time.sleep(app.config["OFF_RETRY_DELAY_SECONDS"])
                continue
            resp.raise_for_status()
            return resp.json(), None
        except requests.RequestException as exc:
            last_error = str(exc)
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
    Stops at the first non-empty result set, floors out at 2 words so it
    never collapses to a single generic brand name.

    Returns (products_raw, query_used, error).
    """
    cleaned = _clean_query(raw_query)
    words = cleaned.split()
    if not words:
        return [], cleaned, None

    tried = []
    for word_count in range(len(words), 1, -1):
        candidate = " ".join(words[:word_count])
        if candidate in tried:
            continue
        tried.append(candidate)

        data, error = _fetch_from_open_food_facts(app, candidate, page_size)
        if error:
            return None, candidate, error
        products = data.get("products", [])
        if products:
            return products, candidate, None

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
    Returns the first decoded barcode string, or None.
    """
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


def _ocr_text_from_bytes(image_bytes):
    """Local, no network -- fallback for when no barcode is visible
    (e.g. a photo of the front label rather than the back). Open Food
    Facts doesn't offer a public reverse-image / visual product search,
    so this is the best we can do from a photo alone: read the text off
    the label and feed it through the same search path as a typed query.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except UnidentifiedImageError:
        return ""
    return pytesseract.image_to_string(img)


def _best_guess_from_ocr(raw_text):
    """OCR off a label is noisy -- multiple lines, stray symbols, all
    caps. Take the longest alphabetic-ish line as the best single guess
    at the product name, since packaging usually gives the product name
    its own prominent line.
    """
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    candidates = [ln for ln in lines if sum(c.isalpha() for c in ln) >= 3]
    if not candidates:
        return ""
    return max(candidates, key=len)


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


def _run_text_job(app, job):
    products_raw, query_used, error = _progressive_search(app, job["query"], job["page_size"])
    if error:
        return None, error
    products = [_normalize_product(p) for p in products_raw]
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
            return {
                "results": [_normalize_product(product)],
                "match_type": "barcode",
                "note": f"Read barcode {barcode} off the photo.",
            }, None
        # Decoded a barcode, but Open Food Facts has no record of it --
        # fall through to OCR rather than dead-ending.

    raw_text = _ocr_text_from_bytes(image_bytes)
    guess = _best_guess_from_ocr(raw_text)
    if not guess:
        return {
            "results": [],
            "match_type": "none",
            "note": "Couldn't find a barcode or readable text on that photo -- try the text search, or a clearer photo of the label or barcode.",
        }, None

    products_raw, query_used, error = _progressive_search(app, guess, job["page_size"])
    if error:
        return None, error
    products = [_normalize_product(p) for p in products_raw]
    note = f'Read "{guess}" off the photo' + (
        f' -- searched "{query_used}".' if query_used != _clean_query(guess) else "."
    )
    if not products:
        note = f'Read "{guess}" off the photo, but couldn\'t find a match. Try the text search instead.'
    return {"results": products, "match_type": "ocr", "note": note}, None


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
            else:
                outcome, error = _run_text_job(app, job)

            if error:
                with _jobs_lock:
                    _jobs[job_id].update(status="error", error=error)
                continue

            with _jobs_lock:
                _jobs[job_id].update(
                    status="done",
                    results=outcome["results"],
                    match_type=outcome["match_type"],
                    note=outcome["note"],
                )
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

    def _food_page_context():
        entries = _todays_log_entries()
        total_calories = sum(e.scaled("calories") or 0 for e in entries)
        return {
            "items": _all_food_items(),
            "active_nav": "food",
            "log_entries": entries,
            "calories_today": round(total_calories) if entries else None,
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
            response["results"] = job["results"]
            response["match_type"] = job.get("match_type")
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


app = create_app()

if __name__ == "__main__":
    app.run(debug=app.config.get("DEBUG", True), port=int(os.environ.get("PORT", 5000)))
