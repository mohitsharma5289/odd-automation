"""
Central configuration. EVERYTHING tunable lives here (env-driven) —
no path, threshold, suffix list, or limit may be hardcoded elsewhere.
"""
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Auth ────────────────────────────────────────────────────────────────
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # ── Database / cache ───────────────────────────────────────────────────
    DATABASE_URL: str
    DATABASE_SYNC_URL: str
    REDIS_URL: str

    # ── ILD engine scope / limits ──────────────────────────────────────────
    PRR_NAME_MAX_LEN: int = Field(default=16)
    # PRR scope: rule NAME must START WITH any prefix AND END WITH any
    # suffix (both case-insensitive, comma-separated). Empty prefix list
    # disables the prefix check.
    PRR_SCOPE_PREFIXES: str = Field(default="ild")
    PRR_SCOPE_SUFFIXES: str = Field(default="s6a")
    # RBAR scope: DESTINATION must match prefix+suffix rules that depend on
    # the DRA instance CATEGORY. JSON mapping {category: {prefixes, suffixes}};
    # "default" applies to any category without an explicit entry
    # (Core/IoT/Charging/Layer/DR → orcl+vdea; Policy → jio+pcrf).
    RBAR_SCOPE_RULES: str = Field(default=(
        '{"default": {"prefixes": ["orcl"], "suffixes": ["vdea"]}, '
        '"Policy": {"prefixes": ["jio"], "suffixes": ["pcrf"]}}'
    ))

    # ── Paths (all configurable; defaults match container volumes) ────────
    EXPORT_PATH: str = "/app/exports"
    DUMP_INCOMING_PATH: str = "/app/dumps/incoming"
    DUMP_PROCESSED_PATH: str = "/app/dumps/processed"
    UPLOAD_PATH: str = "/app/uploads/requests"
    DUMP_UPLOAD_PATH: str = "/app/uploads/dumps"

    # ── Dump source (pluggable; "folder" scans DUMP_INCOMING_PATH) ────────
    # Layout "flat": files sit directly in DUMP_INCOMING_PATH and instance is
    #   resolved from the filename; "nested": {dra_type}/{instance_label}/file
    DUMP_SOURCE_TYPE: str = "folder"
    DUMP_SOURCE_LAYOUT: str = "nested"
    # Only used with DUMP_SOURCE_LAYOUT=flat: named groups (?P<dra_type>…) and
    # (?P<instance_label>…) resolve the owning instance from the filename.
    DUMP_FILENAME_INSTANCE_REGEX: str = ""

    # ── Celery ─────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str
    CELERY_SOFT_TIME_LIMIT: int = 1800
    CELERY_TIME_LIMIT: int = 2100

    # ── Redis pub/sub tuning ───────────────────────────────────────────────
    REDIS_RETRY_COUNT: int = 3
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_SOCKET_TIMEOUT: float = 5.0
    REDIS_CONNECT_TIMEOUT: float = 5.0

    # ── Scheduling (HH:MM, IST; DUMP_INGEST_TIMES is comma-separated) ─────
    RECON_CRON_TIME: str = "02:00"
    DUMP_INGEST_TIMES: str = "06:00,18:00"

    # ── API / CORS / pagination ────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost"
    MAX_PAGE_SIZE: int = 200

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
