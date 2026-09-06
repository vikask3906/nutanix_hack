import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-a-real-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "core",
    "graph",
    "engine",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "cascade.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "cascade.wsgi.application"
ASGI_APPLICATION = "cascade.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "cascade"),
        "USER": os.environ.get("POSTGRES_USER", "cascade"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "cascade"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    # Runs accumulate without bound - an unpaginated list endpoint is a slow
    # outage waiting to happen once a demo has been running for an hour.
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 25,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)-7s %(name)s | %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

# ---------------------------------------------------------------------------
# Cascade engine tuning
# ---------------------------------------------------------------------------

# How long a worker may hold a claimed task before the reaper assumes it died.
# Must comfortably exceed the slowest step you expect to run.
TASK_LEASE_SECONDS = int(os.environ.get("TASK_LEASE_SECONDS", "30"))

# How often a worker renews the lease on a task it is still working on.
TASK_HEARTBEAT_SECONDS = int(os.environ.get("TASK_HEARTBEAT_SECONDS", "10"))

# Idle sleep when the queue is empty, in seconds.
WORKER_POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "1.0"))

# How often the reaper looks for leases that have expired.
REAPER_POLL_SECONDS = float(os.environ.get("REAPER_POLL_SECONDS", "5.0"))

# Absolute ceiling on how many times one step may be attempted, regardless of
# what its retry block says. This is the poison-task guard: a step that reliably
# kills the worker executing it would otherwise be recovered by the reaper
# forever, taking a worker down each time. Ten strikes and the run fails.
MAX_TASK_ATTEMPTS = int(os.environ.get("MAX_TASK_ATTEMPTS", "10"))
