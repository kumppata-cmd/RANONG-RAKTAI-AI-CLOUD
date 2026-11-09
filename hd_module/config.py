"""HD Smart Monitoring — Configuration & Feature Flags."""

import os


class HdConfig:
    """Configuration for the HD Smart Monitoring module."""

    # ── Feature Flag ──────────────────────────────────────
    HD_MODULE_ENABLED = os.environ.get("HD_MODULE_ENABLED", "false").lower() == "true"

    # ── Database ──────────────────────────────────────────
    HD_DATABASE_URL = os.environ.get(
        "HD_DATABASE_URL",
        os.environ.get("DATABASE_URL", ""),
    )

    # ── JWT / Auth ────────────────────────────────────────
    JWT_SECRET = os.environ.get("HD_JWT_SECRET", os.environ.get("ADMIN_SECRET", "dev-only-secret"))
    JWT_ALGORITHM = "HS256"
    JWT_EXPIRY_MINUTES = int(os.environ.get("HD_JWT_EXPIRY_MINUTES", "720"))  # 12 hours (nurse shift)
    SESSION_TIMEOUT_MINUTES = int(os.environ.get("HD_SESSION_TIMEOUT", "30"))

    # ── Clinical Alert Defaults (configurable per-clinic) ─
    ALERT_IDH_SBP_DROP = int(os.environ.get("HD_ALERT_IDH_SBP_DROP", "20"))
    ALERT_IDH_WINDOW_MINUTES = int(os.environ.get("HD_ALERT_IDH_WINDOW", "30"))
    ALERT_SEVERE_IDH_SBP = int(os.environ.get("HD_ALERT_SEVERE_IDH_SBP", "90"))
    ALERT_HIGH_UF_RATE = float(os.environ.get("HD_ALERT_HIGH_UF_RATE", "13.0"))  # mL/kg/hr
    ALERT_EXCESS_WEIGHT_PCT = float(os.environ.get("HD_ALERT_EXCESS_WEIGHT", "4.0"))  # %DW
    ALERT_BRADYCARDIA_HR = int(os.environ.get("HD_ALERT_BRADY_HR", "50"))
    ALERT_TACHYCARDIA_HR = int(os.environ.get("HD_ALERT_TACHY_HR", "120"))
    ALERT_MISSING_DATA_MINUTES = int(os.environ.get("HD_ALERT_MISSING_MIN", "60"))

    # ── Pagination ────────────────────────────────────────
    DEFAULT_PAGE_SIZE = 20
    MAX_PAGE_SIZE = 100

    # ── Bed count ─────────────────────────────────────────
    TOTAL_BEDS = int(os.environ.get("HD_TOTAL_BEDS", "25"))
