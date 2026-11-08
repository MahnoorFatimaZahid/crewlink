"""
CrewLink settings. Everything environment-specific comes from env vars,
so the same image runs locally, in Docker, and as two instances behind nginx.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "core",
    "announcements",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ]},
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "crewlink"),
        "USER": os.environ.get("POSTGRES_USER", "crewlink"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "crewlink"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

# Shared by every instance (Rule 2 and the count cache must not live in one process).
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Without this Django falls back to a per-process memory cache, so two instances would
# each keep their own count cache and login rate-limit counters.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}
# Local dev without Redis only: set REDIS_URL="" (or DJANGO_LOCMEM_CACHE=1), and run
# `manage.py process_sends` instead of Celery. Per-process memory is fine for ONE process;
# never do this in Docker or with more than one instance.
if not REDIS_URL or os.environ.get("DJANGO_LOCMEM_CACHE") == "1":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Celery (Stage 6). Jobs are only messengers: the work to do lives in Postgres,
# so a lost job is rebuilt by the sweeper.
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_IGNORE_RESULT = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BEAT_SCHEDULE = {"sweeper": {"task": "announcements.tasks.sweep_task", "schedule": 60.0}}

# Simulated push provider: fail some pushes / slow it down, to watch retries work.
PUSH_FAIL_RATE = float(os.environ.get("PUSH_FAIL_RATE", "0"))
PUSH_DELAY_MS = int(os.environ.get("PUSH_DELAY_MS", "0"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"crewlink": {"handlers": ["console"], "level": "INFO"}},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "static/"

REST_FRAMEWORK = {
    # Tokens live in Postgres, so a token issued by instance 1 works on instance 2.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    # Rule 1, "the endpoint added next year": deny by default.
    # A view is reachable only if it explicitly declares who may use it.
    "DEFAULT_PERMISSION_CLASSES": ["core.permissions.DenyAll"],
    # Counted in the shared Redis cache, so the limit holds across instances.
    "DEFAULT_THROTTLE_RATES": {"login": "10/min"},
}
