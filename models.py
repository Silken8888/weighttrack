from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class FoodItem(db.Model):
    """A single entry in the user's personal food library.

    Looked up once (Open Food Facts today, USDA FoodData Central planned
    as a fallback) and confirmed by the user, then reused instantly by
    nickname from then on -- no repeat network lookups.
    """

    __tablename__ = "food_items"

    id = db.Column(db.Integer, primary_key=True)

    nickname = db.Column(db.String(120), nullable=False, unique=True, index=True)
    product_name = db.Column(db.String(255), nullable=False)
    brand = db.Column(db.String(255))
    barcode = db.Column(db.String(64), index=True)
    photo_url = db.Column(db.String(500))
    serving_description = db.Column(db.String(255))

    calories = db.Column(db.Float)
    protein_g = db.Column(db.Float)
    carbs_g = db.Column(db.Float)
    fat_g = db.Column(db.Float)
    fiber_g = db.Column(db.Float)
    sugar_g = db.Column(db.Float)
    sodium_mg = db.Column(db.Float)  # stored as mg -- converted from Open
                                      # Food Facts' grams at save time

    source = db.Column(db.String(32), default="open_food_facts")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "nickname": self.nickname,
            "product_name": self.product_name,
            "brand": self.brand,
            "barcode": self.barcode,
            "photo_url": self.photo_url,
            "serving_description": self.serving_description,
            "calories": self.calories,
            "protein_g": self.protein_g,
            "carbs_g": self.carbs_g,
            "fat_g": self.fat_g,
            "fiber_g": self.fiber_g,
            "sugar_g": self.sugar_g,
            "sodium_mg": self.sodium_mg,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")


class FoodLogEntry(db.Model):
    """One entry on the daily timeline: a food from the library, eaten at
    a particular time. Calories/macros are computed from the linked
    FoodItem x servings at read time rather than copied in, so the
    timeline always reflects the library's current numbers -- simpler
    than snapshotting, and fine for a single-user personal app.
    """

    __tablename__ = "food_log_entries"

    id = db.Column(db.Integer, primary_key=True)
    food_item_id = db.Column(db.Integer, db.ForeignKey("food_items.id"), nullable=False)
    food_item = db.relationship("FoodItem")

    meal_type = db.Column(db.String(16), nullable=False)  # one of MEAL_TYPES
    servings = db.Column(db.Float, nullable=False, default=1.0)

    # Stored in UTC, like created_at elsewhere. "Today" is computed against
    # UTC day boundaries -- fine for a single personal user, but means an
    # entry logged right around midnight local time could land on the
    # "wrong" day until the app knows the user's timezone.
    logged_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def scaled(self, field):
        base = getattr(self.food_item, field, None)
        if base is None:
            return None
        return round(base * self.servings, 1)

    @property
    def calories(self):
        return self.scaled("calories")

    @property
    def protein_g(self):
        return self.scaled("protein_g")

    @property
    def carbs_g(self):
        return self.scaled("carbs_g")

    @property
    def fat_g(self):
        return self.scaled("fat_g")

    def to_dict(self):
        return {
            "id": self.id,
            "food_item_id": self.food_item_id,
            "nickname": self.food_item.nickname if self.food_item else None,
            "product_name": self.food_item.product_name if self.food_item else None,
            "photo_url": self.food_item.photo_url if self.food_item else None,
            "serving_description": self.food_item.serving_description if self.food_item else None,
            "meal_type": self.meal_type,
            "servings": self.servings,
            "logged_at": self.logged_at.isoformat() if self.logged_at else None,
            "calories": self.scaled("calories"),
            "protein_g": self.scaled("protein_g"),
            "carbs_g": self.scaled("carbs_g"),
            "fat_g": self.scaled("fat_g"),
        }
