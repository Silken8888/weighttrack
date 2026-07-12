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
    """One entry on the daily timeline, one of two shapes:

    - Library-linked: food_item_id points at a catalogued FoodItem: precise
      numbers, scaled by servings.
    - Photo-logged: food_item_id is null; photo_url/description/ai_calories
      come from a snapped photo of a home-cooked or unpackaged meal, run
      through Claude's vision API for a rough estimate. manual_calories, if
      set, always wins -- that's the "manual adjustment field right next to
      it" the AI estimate needs, since vision can't judge portion size or
      hidden oil/butter.
    """

    __tablename__ = "food_log_entries"

    id = db.Column(db.Integer, primary_key=True)
    food_item_id = db.Column(db.Integer, db.ForeignKey("food_items.id"), nullable=True)
    food_item = db.relationship("FoodItem")

    meal_type = db.Column(db.String(16), nullable=False)  # one of MEAL_TYPES
    servings = db.Column(db.Float, nullable=False, default=1.0)

    # Stored in UTC, like created_at elsewhere. "Today" is computed against
    # UTC day boundaries -- fine for a single personal user, but means an
    # entry logged right around midnight local time could land on the
    # "wrong" day until the app knows the user's timezone.
    logged_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    photo_url = db.Column(db.String(500))
    description = db.Column(db.String(200))
    ai_calories = db.Column(db.Float)
    ai_protein_g = db.Column(db.Float)
    ai_carbs_g = db.Column(db.Float)
    ai_fat_g = db.Column(db.Float)
    manual_calories = db.Column(db.Float)

    # Groups entries logged together in one agent submission (e.g. "toast
    # + peanut butter + coffee + creamer" all become separate rows, but
    # share a batch_id), so "repeat this meal" can clone the whole group
    # at once instead of one item at a time.
    batch_id = db.Column(db.String(32), index=True)

    def scaled(self, field):
        base = getattr(self.food_item, field, None)
        if base is None:
            return None
        return round(base * self.servings, 1)

    @property
    def calories(self):
        if self.manual_calories is not None:
            return self.manual_calories
        if self.food_item_id and self.food_item is not None:
            return self.scaled("calories")
        return self.ai_calories

    @property
    def protein_g(self):
        if self.food_item_id and self.food_item is not None:
            return self.scaled("protein_g")
        return self.ai_protein_g

    @property
    def carbs_g(self):
        if self.food_item_id and self.food_item is not None:
            return self.scaled("carbs_g")
        return self.ai_carbs_g

    @property
    def fat_g(self):
        if self.food_item_id and self.food_item is not None:
            return self.scaled("fat_g")
        return self.ai_fat_g

    @property
    def display_name(self):
        if self.food_item is not None:
            return self.food_item.product_name
        return self.description or "Photo-Logged Meal"

    @property
    def display_photo_url(self):
        if self.photo_url:
            return self.photo_url
        if self.food_item is not None:
            return self.food_item.photo_url
        return None

    @property
    def is_photo_logged(self):
        return self.food_item_id is None

    def to_dict(self):
        return {
            "id": self.id,
            "food_item_id": self.food_item_id,
            "nickname": self.food_item.nickname if self.food_item else None,
            "product_name": self.display_name,
            "photo_url": self.display_photo_url,
            "serving_description": self.food_item.serving_description if self.food_item else None,
            "meal_type": self.meal_type,
            "servings": self.servings,
            "logged_at": self.logged_at.isoformat() if self.logged_at else None,
            "calories": self.calories,
            "ai_calories": self.ai_calories,
            "manual_calories": self.manual_calories,
            "protein_g": self.protein_g,
            "carbs_g": self.carbs_g,
            "fat_g": self.fat_g,
            "source": "photo" if self.is_photo_logged else "library",
            "batch_id": self.batch_id,
        }


class WeighIn(db.Model):
    """One weight entry. US units throughout (pounds) -- this is a body
    weight tracker for a US-based user, unlike the nutrition figures
    (which stay in grams because that's what real US FDA labels use)."""

    __tablename__ = "weigh_ins"

    id = db.Column(db.Integer, primary_key=True)
    weight_lbs = db.Column(db.Float, nullable=False)
    notes = db.Column(db.String(280))  # sleep/stress/alcohol context, per the original spec
    logged_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "weight_lbs": self.weight_lbs,
            "notes": self.notes,
            "logged_at": self.logged_at.isoformat() if self.logged_at else None,
        }


class VacationPeriod(db.Model):
    """A date range that doesn't break the weigh-in streak even without
    an entry logged -- per the original spec, travel shouldn't zero out
    an established streak."""

    __tablename__ = "vacation_periods"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(120))
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)

    def contains(self, date_):
        return self.start_date <= date_ <= self.end_date

    def to_dict(self):
        return {
            "id": self.id,
            "label": self.label,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
        }


ACTIVITY_LEVELS = {
    "sedentary": ("Sedentary", 1.2),
    "light": ("Lightly Active", 1.375),
    "moderate": ("Moderately Active", 1.55),
    "active": ("Very Active", 1.725),
    "very_active": ("Extremely Active", 1.9),
}


class UserProfile(db.Model):
    """Single-row settings table -- this is a personal, single-user app,
    so there's exactly one profile (id=1), used to compute a daily
    calorie target via the Mifflin-St Jeor equation."""

    __tablename__ = "user_profile"

    id = db.Column(db.Integer, primary_key=True)
    height_in = db.Column(db.Float)
    age = db.Column(db.Integer)
    biological_sex = db.Column(db.String(10))  # "male" | "female" -- Mifflin-St Jeor needs this
    activity_level = db.Column(db.String(20), default="sedentary")

    def is_complete(self):
        return bool(self.height_in and self.age and self.biological_sex)

    def calorie_target(self, weight_lbs):
        """Mifflin-St Jeor: the standard, well-validated formula for
        estimating daily calorie needs from body measurements. Needs
        weight, height, age, and biological sex -- returns None if
        anything's missing rather than guessing.
        """
        if not (self.is_complete() and weight_lbs):
            return None
        weight_kg = weight_lbs * 0.453592
        height_cm = self.height_in * 2.54
        if self.biological_sex == "male":
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * self.age + 5
        else:
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * self.age - 161
        _, factor = ACTIVITY_LEVELS.get(self.activity_level, ACTIVITY_LEVELS["sedentary"])
        return round(bmr * factor)

    def to_dict(self):
        return {
            "height_in": self.height_in,
            "age": self.age,
            "biological_sex": self.biological_sex,
            "activity_level": self.activity_level,
        }


class ExerciseEntry(db.Model):
    """A manually logged exercise session (activity + calories burned),
    feeding into the Dashboard's intake-vs-target comparison."""

    __tablename__ = "exercise_entries"

    id = db.Column(db.Integer, primary_key=True)
    activity = db.Column(db.String(120), nullable=False)
    calories_burned = db.Column(db.Float, nullable=False)
    logged_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "activity": self.activity,
            "calories_burned": self.calories_burned,
            "logged_at": self.logged_at.isoformat() if self.logged_at else None,
        }
