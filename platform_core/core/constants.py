# FILE: platform_core/core/constants.py
"""
platform_core.core.constants
═══════════════════════════════════════════════════════════════════════
Core constants for the institutional derivatives intelligence and
automated execution platform — Indian Index F&O specialization.

Design constraints preserved from Phases 1–9A safeguards:
  - Full package imports; no local-relative imports.
  - Exchange-sensitive operational values (lot sizes, strike steps) are
    present as REFERENCE DEFAULTS only and must be overridden by the
    config layer (Phase 9A config system) for live environments.
  - BarInterval references use explicit SEC_1/MIN_1/… names.
  - All constants are JSON-serializable, Redis-compatible, and
    TimescaleDB-compatible (no runtime-only objects except timezone and
    Decimal, which serialize cleanly).
  - No business logic, no service imports, no circular dependencies.

Sections:
  1. Timezone and session anchors
  2. Numeric primitives and sentinels
  3. Market reference defaults  (CONFIGURABLE IN LIVE ENVIRONMENTS)
  4. Bar and windowing defaults
  5. Streaming, replay, and event defaults
  6. Validation / quality-control defaults
  7. Persistence defaults
  8. Redis keyspace conventions
  9. File-system conventions
  10. Observability and safety defaults
  11. Options analytics bounds
  12. Safety-critical execution defaults
  13. Platform versioning
"""
from __future__ import annotations

from datetime import time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from platform_core.core.enums import BarInterval, Exchange, UnderlyingFamily

# ── 1. Timezone and session anchors ───────────────────────────────────────────

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

#: NSE normal market open (9:15 IST)
NSE_SESSION_OPEN: time = time(hour=9, minute=15, tzinfo=IST)
#: NSE normal market close (15:30 IST)
NSE_SESSION_CLOSE: time = time(hour=15, minute=30, tzinfo=IST)
#: Pre-open session begins (9:00 IST)
PRE_OPEN_START: time = time(hour=9, minute=0, tzinfo=IST)
#: Post-close cut-off for chain analytics (16:00 IST)
POST_CLOSE_END: time = time(hour=16, minute=0, tzinfo=IST)

SECONDS_PER_MINUTE: int = 60
MILLISECONDS_PER_SECOND: int = 1_000
MICROSECONDS_PER_SECOND: int = 1_000_000
NANOSECONDS_PER_SECOND: int = 1_000_000_000

# ── 2. Numeric primitives and sentinels ───────────────────────────────────────

DECIMAL_ZERO: Decimal = Decimal("0")
DECIMAL_ONE: Decimal  = Decimal("1")
PRICE_EPSILON: Decimal = Decimal("0.01")
BPS_DIVISOR: Decimal  = Decimal("10000")
PERCENT_DIVISOR: Decimal = Decimal("100")

#: Sentinel values for missing data — safe for DB NULL mapping
MISSING_INT: int     = -1
MISSING_FLOAT: float = float("nan")
MISSING_STR: str     = ""
MISSING_DECIMAL: Decimal = Decimal("-1")

DEFAULT_DECIMAL_QUANT: Decimal = Decimal("0.0001")
PRICE_DECIMAL_QUANT: Decimal   = Decimal("0.01")
IV_DECIMAL_QUANT: Decimal      = Decimal("0.0001")
RATIO_DECIMAL_QUANT: Decimal   = Decimal("0.000001")

# ── 3. Market reference defaults (CONFIGURABLE) ───────────────────────────────
#
# WARNING: These are REFERENCE DEFAULTS for the foundational phase only.
# Exchange circulars revise lot sizes and strike steps periodically.
# The config system (platform_core/core/config.py) MUST override these
# from instrument-master data before any live order submission.
#

DEFAULT_EXCHANGE: Exchange = Exchange.NSE

SUPPORTED_EXCHANGES: frozenset[Exchange] = frozenset({
    Exchange.NSE,
    Exchange.BSE,
})

SUPPORTED_UNDERLYINGS: frozenset[UnderlyingFamily] = frozenset({
    UnderlyingFamily.NIFTY,
    UnderlyingFamily.BANKNIFTY,
    UnderlyingFamily.FINNIFTY,
    UnderlyingFamily.SENSEX,
})

#: Reference strike steps per underlying (INR). Override via config.
REFERENCE_STRIKE_STEPS: dict[UnderlyingFamily, Decimal] = {
    UnderlyingFamily.NIFTY:     Decimal("50"),
    UnderlyingFamily.BANKNIFTY: Decimal("100"),
    UnderlyingFamily.FINNIFTY:  Decimal("50"),
    UnderlyingFamily.SENSEX:    Decimal("100"),
}

#: Reference lot sizes per underlying (units). Override via config.
REFERENCE_LOT_SIZES: dict[UnderlyingFamily, int] = {
    UnderlyingFamily.NIFTY:     75,
    UnderlyingFamily.BANKNIFTY: 15,
    UnderlyingFamily.FINNIFTY:  25,
    UnderlyingFamily.SENSEX:    10,
}

# ── 4. Bar and windowing defaults ─────────────────────────────────────────────

BAR_INTERVAL_TO_SECONDS: dict[BarInterval, int] = {
    BarInterval.SEC_1:  1,
    BarInterval.MIN_1:  60,
    BarInterval.MIN_5:  300,
    BarInterval.MIN_15: 900,
    BarInterval.MIN_30: 1_800,
    BarInterval.HOUR_1: 3_600,
    BarInterval.DAY_1:  86_400,
}

#: Primary bar interval for signal generation (1-minute finalized bars)
PRIMARY_SIGNAL_BAR_INTERVAL: BarInterval  = BarInterval.MIN_1
#: Secondary bar interval for structure context (5-minute)
SECONDARY_SIGNAL_BAR_INTERVAL: BarInterval = BarInterval.MIN_5
#: Bars required on each side of pivot to confirm a swing point
STRUCTURE_CONFIRMATION_LEFT_SPAN: int  = 2
STRUCTURE_CONFIRMATION_RIGHT_SPAN: int = 2

ATR_DEFAULT_LOOKBACK: int     = 20
MEDIAN_LOOKBACK: int          = 20
VWAP_MIN_VOLUME: int          = 1
HURST_LOOKBACK: int           = 100
ADX_LOOKBACK: int             = 14
GARCH_P: int                  = 1
GARCH_Q: int                  = 1
VRP_LOOKBACK_DAYS: int        = 20
PCR_ZSCORE_LOOKBACK: int      = 20
GEX_STRIKE_RADIUS: int        = 10

# ── 5. Streaming, replay, and event defaults ──────────────────────────────────

EVENT_SCHEMA_VERSION: str  = "1.0.0"
EVENT_ID_PREFIX: str       = "evt"
DEFAULT_EVENT_PRIORITY: str = "NORMAL"
MAX_EVENT_PAYLOAD_BYTES: int = 512_000
DEFAULT_EVENT_BUS_QUEUE_SIZE: int = 10_000

#: Default replay speed multiplier vs. wall clock
DEFAULT_REPLAY_SPEED_FACTOR: float = 10.0
#: Speed factor of 0 → step-through mode (manual advance)
REPLAY_STEP_MODE_SPEED: float  = 0.0
DEFAULT_REPLAY_CHUNK_SIZE: int = 10_000
REPLAY_HEARTBEAT_INTERVAL_SEC: float = 1.0

# ── 6. Validation / quality-control defaults ──────────────────────────────────

#: Maximum acceptable clock drift between feed timestamps and local wall time
MAX_CLOCK_SKEW_MS: int = 500
#: Maximum age of the most recent tick before raising a STALE quality flag
MAX_ALLOWED_STALENESS_MS: int = 3_000
#: Extended stale window allowed during warm-start / reconnect phase
MAX_WARM_START_STALENESS_MS: int = 30_000
#: Minimum fraction of valid strikes in a chain snapshot for acceptance
MIN_CHAIN_SNAPSHOT_QUALITY: Decimal = Decimal("0.50")
#: Maximum relative bid-ask spread ratio before SPREAD_WIDE flag fires
MAX_SPREAD_RATIO_DEFAULT: Decimal = Decimal("0.05")
#: ATM band width in number of strikes used for chain analytics
ATM_BAND_STRIKES_DEFAULT: int = 10
#: Consecutive zero-bid ticks before triggering QUARANTINE action
MAX_ZERO_BID_STREAK: int = 20
#: Storage width reserved for quality flag integers in DB columns
QUALITY_FLAG_STORAGE_BITS: int = 32

# ── 7. Persistence defaults ───────────────────────────────────────────────────

POSTGRES_POOL_MIN_SIZE: int     = 5
POSTGRES_POOL_MAX_SIZE: int     = 20
REDIS_POOL_MAX_CONNECTIONS: int = 20

TIMESCALE_WRITE_BATCH_SIZE: int    = 500
TIMESCALE_FLUSH_INTERVAL_SEC: float = 2.0
WRITER_MAX_BUFFER: int             = 20_000
WRITER_MAX_RETRIES: int            = 3
WRITER_RETRY_BASE_DELAY_SEC: float = 1.0

#: Days of tick data kept in the TimescaleDB hot-tier (recent chunks)
HOT_RETENTION_DAYS: int = 14
#: Bar interval used for chain snapshot storage cadence
CHAIN_SNAPSHOT_INTERVAL: BarInterval = BarInterval.MIN_1

# ── 8. Redis keyspace conventions ─────────────────────────────────────────────

REDIS_KEY_SEPARATOR: str      = ":"
REDIS_NAMESPACE_DEFAULT: str  = "platform"

#: Key-component tokens (combined with namespace + separator by cache layer)
REDIS_KEY_QUOTE:     str = "quote"
REDIS_KEY_ATM:       str = "atm"
REDIS_KEY_REGIME:    str = "regime"
REDIS_KEY_RISK:      str = "risk"
REDIS_KEY_HEARTBEAT: str = "heartbeat"
REDIS_KEY_LOCK:      str = "lock"

#: TTL values (seconds) for Redis entries
REDIS_TTL_QUOTE_SEC:      int = 1_800   # 30 minutes
REDIS_TTL_HEARTBEAT_SEC:  int = 30
#: Idempotency lock expiry (milliseconds) for order deduplication
REDIS_TTL_IDEMPOTENCY_LOCK_MS: int = 5_000

# ── 9. File-system conventions ────────────────────────────────────────────────

PROJECT_ROOT_MARKER: str = "platform_core"
OUTPUT_DIRNAME:      str = "output"
DLQ_DIRNAME:         str = "dlq"
LOG_DIRNAME:         str = "logs"
CONFIG_DIRNAME:      str = "config"
MIGRATIONS_DIRNAME:  str = "migrations"
TESTS_DIRNAME:       str = "tests"

HOME_DIR: Path          = Path.home()
DEFAULT_OUTPUT_DIR: Path = HOME_DIR / OUTPUT_DIRNAME
DEFAULT_DLQ_DIR: Path    = DEFAULT_OUTPUT_DIR / DLQ_DIRNAME
DEFAULT_LOG_DIR: Path    = HOME_DIR / LOG_DIRNAME

# ── 10. Observability and safety defaults ─────────────────────────────────────

#: Interval at which every service emits SERVICE_HEARTBEAT events
HEARTBEAT_INTERVAL_SEC: float = 5.0
#: Maximum time allowed for a service to complete graceful shutdown
SERVICE_SHUTDOWN_TIMEOUT_SEC: float = 15.0
#: HTTP / gRPC health-check response budget
HEALTHCHECK_TIMEOUT_SEC: float = 2.0

#: Latency thresholds for LATENCY_METRIC monitoring events
LATENCY_WARNING_MS:  float = 100.0
LATENCY_CRITICAL_MS: float = 250.0

#: Consecutive exchange order rejections before EXCHANGE_REJECT_STORM fires
REJECT_STORM_THRESHOLD: int = 5
#: Interval between position reconciliation cycles
RECONCILIATION_POLL_INTERVAL_SEC: float = 30.0

# ── 11. Options analytics bounds ─────────────────────────────────────────────

IV_MIN: Decimal = Decimal("0.0001")
IV_MAX: Decimal = Decimal("5.0000")   # 500% IV upper bound
DELTA_MIN: Decimal = Decimal("-1.0")
DELTA_MAX: Decimal = Decimal("1.0")
GAMMA_MIN: Decimal = Decimal("0.0")
VEGA_MIN:  Decimal = Decimal("0.0")
THETA_MAX_ABS: Decimal = Decimal("1000000")
RHO_MAX_ABS:   Decimal = Decimal("1000000")

#: Volatility Risk Premium significance threshold (positive = sellers edge)
VRP_SIGNIFICANCE_THRESHOLD: Decimal = Decimal("0.02")
#: PCR Z-score extremity threshold triggering OI analytics alert
PCR_ZSCORE_EXTREMITY: Decimal = Decimal("1.5")
#: Displacement impulse threshold vs. 20-bar ATR
DISPLACEMENT_ATR_MULTIPLIER: Decimal = Decimal("1.5")
#: Displacement volume threshold vs. 20-bar mean volume
DISPLACEMENT_VOLUME_SIGMA: Decimal = Decimal("2.0")

# ── 12. Safety-critical execution defaults ────────────────────────────────────

#: Deduplication window — orders with same intent within this window are blocked
ORDER_IDEMPOTENCY_WINDOW_SEC: int = 30
#: Maximum allowed time for emergency position flatten
EMERGENCY_FLATTEN_TIMEOUT_SEC: float = 10.0
#: Maximum exchange-level retries per order before marking FAILED
MAX_ORDER_RETRY_ATTEMPTS: int = 3
#: Minimum gap between consecutive order submissions (milliseconds)
MIN_EXECUTION_COOLDOWN_MS: int = 250

# ── 13. Platform versioning ────────────────────────────────────────────────────

PLATFORM_NAME: str          = "institutional-derivatives-intelligence-platform"
PLATFORM_CODE: str          = "IDIP"
ARCHITECTURE_VERSION: str   = "9A"
API_VERSION: str            = "1"

# Expose all uppercase names via __all__ for star-import consumers
__all__: list[str] = [name for name in globals() if name.isupper() or name[0].isupper()]
