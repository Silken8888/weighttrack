import os


class Config:
    """Base Flask configuration.

    DigitalOcean's managed Postgres connection string starts with
    "postgres://", but modern SQLAlchemy (via psycopg2) requires
    "postgresql://". We rewrite it here -- once, at config load time --
    rather than in app.py, so every entry point (web process, shell,
    future worker) gets the same corrected URL.
    """

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

    _raw_db_url = os.environ.get("DATABASE_URL", "sqlite:///weighttrack.db")
    if _raw_db_url.startswith("postgres://"):
        _raw_db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _raw_db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Open Food Facts -- free, no key needed for reads, but a real
    # User-Agent is required or requests get throttled/rejected.
    # The legacy /cgi/search.pl endpoint is the one confirmed working via
    # live test; the newer search.openfoodfacts.org and /api/v2/search
    # endpoints returned 502/503 during testing.
    OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
    # Exact-match endpoint used when we've decoded a real barcode (from a
    # photo) -- far more reliable than text search when we have one.
    OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
    OFF_USER_AGENT = "WeighTrack/1.0 (personal health tracker; contact: weighttrack-app@example.com)"
    OFF_RETRY_COUNT = 1
    OFF_RETRY_DELAY_SECONDS = 1.5
    OFF_REQUEST_TIMEOUT_SECONDS = 8

    # Photo-based product lookup (barcode decode, OCR fallback)
    MAX_PHOTO_BYTES = 8 * 1024 * 1024  # 8 MB
    ALLOWED_PHOTO_MIMETYPES = {"image/jpeg", "image/png", "image/webp"}

    # Background search jobs -- see app.py. Finished jobs are pruned from
    # the in-memory store after this many seconds.
    SEARCH_JOB_TTL_SECONDS = 300


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
}
