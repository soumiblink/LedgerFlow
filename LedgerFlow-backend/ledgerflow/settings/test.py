"""
Test settings — uses local SQLite so tests are fast, isolated,
and don't touch the Neon cloud database.
"""
from .base import *  # noqa

# Override DB with SQLite for tests
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_db.sqlite3",
    }
}

# Disable Celery task queueing during tests — call logic directly
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Faster password hashing in tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]
