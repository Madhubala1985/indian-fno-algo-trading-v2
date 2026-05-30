# FILE: platform_core/core/enums.py

"""
platform_core.core.enums
═══════════════════════════════════════════════════════════════════════
Core enumerations for the institutional derivatives intelligence and
automated execution platform — Indian Index F&O specialization.

Architecture constraints preserved from Phases 1–9A:
  - StrEnum  : safe JSON / Redis / PostgreSQL / TimescaleDB serialization.
  - IntFlag  : bitwise composition for quality flags and safety state.
  - Explicit bar-interval names: no M1/H1/D1 to prevent clashes with
    pandas frequencies, futures notation, and ML feature namespacing.
  - Every enum is tagged @unique to prevent accidental value aliasing.
  - No business logic; no service imports; no circular dependencies.
  - Event types remain grouped by domain for future modular split into
    platform_core/events/{market,execution,risk,replay}_events.py.
"""

from __future__ import annotations

from enum import IntFlag, StrEnum, unique


# ─────────────────────────────────────────────────────────────────────────────
# Runtime environment
# ─────────────────────────────────────────────────────────────────────────────

@unique
class Environment(StrEnum):
    """Deployment environment tier."""

    LOCAL = "LOCAL"
    DEV = "DEV"
    UAT = "UAT"
    PROD = "PROD"


# ─────────────────────────────────────────────────────────────────────────────
# Exchange / instrument taxonomy
# ─────────────────────────────────────────────────────────────────────────────

@unique
class Exchange(StrEnum):
    """Supported exchange venues."""

    NSE = "NSE"
    BSE = "BSE"


@unique
class Segment(StrEnum):
    """Exchange market segments."""

    CASH = "CASH"
    FUT = "FUT"
    OPT = "OPT"


@unique
class AssetClass(StrEnum):
    """High-level asset classification."""

    EQUITY = "EQUITY"
    INDEX = "INDEX"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    FOREX = "FOREX"
    COMMODITY = "COMMODITY"


@unique
class UnderlyingFamily(StrEnum):
    """Indian index underlyings validated in Phase 1 research."""

    NIFTY = "NIFTY"
    BANKNIFTY = "BANKNIFTY"
    FINNIFTY = "FINNIFTY"
    SENSEX = "SENSEX"


@unique
class InstrumentType(StrEnum):
    """Canonical instrument taxonomy."""

    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


@unique
class OptionType(StrEnum):
    """Option right."""

    CALL = "CALL"
    PUT = "PUT"


@unique
class ExpiryType(StrEnum):
    """Contract expiry classification."""

    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


@unique
class MarketDataType(StrEnum):
    """Normalized market data event categories."""

    TICK = "TICK"
    QUOTE = "QUOTE"
    TRADE = "TRADE"
    BAR = "BAR"
    OPTION_CHAIN = "OPTION_CHAIN"


# ─────────────────────────────────────────────────────────────────────────────
# Bar intervals
# ─────────────────────────────────────────────────────────────────────────────

@unique
class BarInterval(StrEnum):
    """
    Finalized bar intervals.

    Explicit names only. M1 / H1 / D1 intentionally avoided.
    """

    SEC_1 = "1s"
    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    MIN_30 = "30m"
    HOUR_1 = "1h"
    DAY_1 = "1d"


# ─────────────────────────────────────────────────────────────────────────────
# Market session
# ─────────────────────────────────────────────────────────────────────────────

@unique
class MarketSessionPhase(StrEnum):
    """Exchange session lifecycle phases."""

    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    POST_CLOSE = "POST_CLOSE"
    CLOSED = "CLOSED"
    HALTED = "HALTED"


# ─────────────────────────────────────────────────────────────────────────────
# Event system
# ─────────────────────────────────────────────────────────────────────────────

@unique
class EventDomain(StrEnum):
    """High-level event namespace boundaries."""

    SYSTEM = "SYSTEM"
    MARKET_DATA = "MARKET_DATA"
    CHAIN = "CHAIN"
    ANALYTICS = "ANALYTICS"
    REGIME = "REGIME"
    SIGNAL = "SIGNAL"
    RISK = "RISK"
    ORDER = "ORDER"
    EXECUTION = "EXECUTION"
    PORTFOLIO = "PORTFOLIO"
    REPLAY = "REPLAY"
    MONITORING = "MONITORING"


@unique
class EventType(StrEnum):
    """Canonical event type tokens."""

    SERVICE_STARTED = "SERVICE_STARTED"
    SERVICE_STOPPED = "SERVICE_STOPPED"
    SERVICE_HEARTBEAT = "SERVICE_HEARTBEAT"
    CONFIG_RELOADED = "CONFIG_RELOADED"

    RAW_TICK_RECEIVED = "RAW_TICK_RECEIVED"
    NORMALIZED_TICK = "NORMALIZED_TICK"
    QUOTE_TICK = "QUOTE_TICK"
    TRADE_TICK = "TRADE_TICK"
    HEARTBEAT_TICK = "HEARTBEAT_TICK"
    BAR_CLOSED = "BAR_CLOSED"
    FEED_STALE = "FEED_STALE"
    FEED_RECONNECTED = "FEED_RECONNECTED"

    CHAIN_SNAPSHOT = "CHAIN_SNAPSHOT"
    CHAIN_SNAPSHOT_VALIDATED = "CHAIN_SNAPSHOT_VALIDATED"
    ATM_REMAP = "ATM_REMAP"

    SWING_CONFIRMED = "SWING_CONFIRMED"
    BOS_DETECTED = "BOS_DETECTED"
    CHOCH_DETECTED = "CHOCH_DETECTED"
    LIQUIDITY_SWEEP_DETECTED = "LIQUIDITY_SWEEP_DETECTED"
    DISPLACEMENT_DETECTED = "DISPLACEMENT_DETECTED"
    FVG_CREATED = "FVG_CREATED"
    FVG_UPDATED = "FVG_UPDATED"
    SUPPORT_RESISTANCE_UPDATED = "SUPPORT_RESISTANCE_UPDATED"

    GREEKS_UPDATED = "GREEKS_UPDATED"
    IV_UPDATED = "IV_UPDATED"
    IV_SURFACE_UPDATED = "IV_SURFACE_UPDATED"
    OI_ANALYTICS_UPDATED = "OI_ANALYTICS_UPDATED"
    DEALER_HEDGING_UPDATED = "DEALER_HEDGING_UPDATED"
    GEX_UPDATED = "GEX_UPDATED"
    PREMIUM_ANALYTICS_UPDATED = "PREMIUM_ANALYTICS_UPDATED"

    REGIME_UPDATED = "REGIME_UPDATED"
    FEATURE_VECTOR_READY = "FEATURE_VECTOR_READY"
    SIGNAL_EMITTED = "SIGNAL_EMITTED"
    CONFIDENCE_UPDATED = "CONFIDENCE_UPDATED"

    RISK_STATE_UPDATED = "RISK_STATE_UPDATED"
    KILL_SWITCH_TRIGGERED = "KILL_SWITCH_TRIGGERED"
    EMERGENCY_SHUTDOWN_TRIGGERED = "EMERGENCY_SHUTDOWN_TRIGGERED"

    ORDER_INTENT_CREATED = "ORDER_INTENT_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACKNOWLEDGED = "ORDER_ACKNOWLEDGED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REPLACED = "ORDER_REPLACED"
    POSITION_UPDATED = "POSITION_UPDATED"
    PORTFOLIO_UPDATED = "PORTFOLIO_UPDATED"
    RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"

    REPLAY_STARTED = "REPLAY_STARTED"
    REPLAY_PAUSED = "REPLAY_PAUSED"
    REPLAY_RESUMED = "REPLAY_RESUMED"
    REPLAY_COMPLETED = "REPLAY_COMPLETED"

    VALIDATION_WARNING = "VALIDATION_WARNING"
    VALIDATION_REJECT = "VALIDATION_REJECT"
    DATA_QUALITY_INCIDENT = "DATA_QUALITY_INCIDENT"
    LATENCY_METRIC = "LATENCY_METRIC"


@unique
class EventPriority(StrEnum):
    """Priority hints for event routing."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ─────────────────────────────────────────────────────────────────────────────
# Data quality
# ─────────────────────────────────────────────────────────────────────────────

@unique
class DataQualityAction(StrEnum):
    """Validation escalation actions."""

    ACCEPT = "ACCEPT"
    REPAIR = "REPAIR"
    ACCEPT_WITH_PENALTY = "ACCEPT_WITH_PENALTY"
    QUARANTINE = "QUARANTINE"
    REJECT = "REJECT"


class TickQualityFlag(IntFlag):
    """Bitwise tick quality flags."""

    NONE = 0
    STALE = 1 << 0
    ZERO_BID = 1 << 1
    ZERO_ASK = 1 << 2
    SPREAD_WIDE = 1 << 3
    BAD_SEQUENCE = 1 << 4
    CLOCK_SKEW = 1 << 5
    OUTSIDE_SESSION = 1 << 6
    MISSING_OI = 1 << 7
    MISSING_VOLUME = 1 << 8
    MISSING_PRICE = 1 << 9
    DERIVED_VALUE = 1 << 10
    REPAIRED_VALUE = 1 << 11
    QUARANTINED = 1 << 12


class SafeStateFlag(IntFlag):
    """Platform-wide operational safety-state flags."""

    NONE = 0
    MARKET_DATA_DEGRADED = 1 << 0
    VALIDATION_DEGRADED = 1 << 1
    REPLAY_MODE = 1 << 2
    EXECUTION_DISABLED = 1 << 3
    KILL_SWITCH_ACTIVE = 1 << 4
    EMERGENCY_SHUTDOWN = 1 << 5
    RISK_LIMIT_BREACH = 1 << 6
    LATENCY_BREACH = 1 << 7
    RECONCILIATION_REQUIRED = 1 << 8


# ─────────────────────────────────────────────────────────────────────────────
# Market structure
# ─────────────────────────────────────────────────────────────────────────────

@unique
class StructureBias(StrEnum):
    """Directional market structure state."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@unique
class BreakKind(StrEnum):
    """Structure break classification."""

    BOS = "BOS"
    CHOCH = "CHOCH"


@unique
class SwingKind(StrEnum):
    """Confirmed pivot classification."""

    HIGH = "HIGH"
    LOW = "LOW"


@unique
class SweepSide(StrEnum):
    """Liquidity sweep directional side."""

    BUYSIDE = "BUYSIDE"
    SELLSIDE = "SELLSIDE"


# ─────────────────────────────────────────────────────────────────────────────
# Regime / ML
# ─────────────────────────────────────────────────────────────────────────────

@unique
class RegimeStateType(StrEnum):
    """Primary market regime labels."""

    TRENDING = "TRENDING"
    MEAN_REVERTING = "MEAN_REVERTING"
    VOL_EXPANSION = "VOL_EXPANSION"
    VOL_COMPRESSION = "VOL_COMPRESSION"
    EXPIRY_DRIVEN = "EXPIRY_DRIVEN"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    UNCLASSIFIED = "UNCLASSIFIED"


@unique
class SignalSide(StrEnum):
    """Directional signal intent."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@unique
class SignalType(StrEnum):
    """Signal family taxonomy."""

    CONTINUATION = "CONTINUATION"
    REVERSAL = "REVERSAL"
    TRAP = "TRAP"
    VOLATILITY = "VOLATILITY"
    HEDGE = "HEDGE"
    EXIT = "EXIT"


# ─────────────────────────────────────────────────────────────────────────────
# Risk
# ─────────────────────────────────────────────────────────────────────────────

@unique
class RiskLevel(StrEnum):
    """Graduated severity for risk events."""

    INFO = "INFO"
    WARNING = "WARNING"
    BREACH = "BREACH"
    CRITICAL = "CRITICAL"


@unique
class KillSwitchReason(StrEnum):
    """Safety-critical kill-switch reason codes."""

    MANUAL = "MANUAL"
    DATA_STALE = "DATA_STALE"
    LATENCY_BREACH = "LATENCY_BREACH"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    CORRELATION_BREACH = "CORRELATION_BREACH"
    LIQUIDITY_FAILURE = "LIQUIDITY_FAILURE"
    EXCHANGE_REJECT_STORM = "EXCHANGE_REJECT_STORM"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    UNKNOWN = "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# Execution
# ─────────────────────────────────────────────────────────────────────────────

@unique
class OrderSide(StrEnum):
    """Order direction."""

    BUY = "BUY"
    SELL = "SELL"


@unique
class OrderType(StrEnum):
    """Supported order types."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL_M"


@unique
class ProductType(StrEnum):
    """Broker product classification."""

    MIS = "MIS"
    NRML = "NRML"
    CNC = "CNC"


@unique
class TimeInForce(StrEnum):
    """Order time-in-force."""

    DAY = "DAY"
    IOC = "IOC"


@unique
class OrderStatus(StrEnum):
    """Normalized order lifecycle state."""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@unique
class PositionSide(StrEnum):
    """Net directional position classification."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


# ─────────────────────────────────────────────────────────────────────────────
# Replay
# ─────────────────────────────────────────────────────────────────────────────

@unique
class ReplayMode(StrEnum):
    """Replay simulation modes."""

    TICK_ACCURATE = "TICK_ACCURATE"
    BAR_SNAPSHOT = "BAR_SNAPSHOT"
    HYBRID = "HYBRID"


@unique
class ReplayState(StrEnum):
    """Replay engine lifecycle state."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ─────────────────────────────────────────────────────────────────────────────
# Storage / infrastructure
# ─────────────────────────────────────────────────────────────────────────────

@unique
class StorageBackend(StrEnum):
    """Persistence backend roles."""

    POSTGRESQL = "POSTGRESQL"
    TIMESCALEDB = "TIMESCALEDB"
    REDIS = "REDIS"
    PARQUET = "PARQUET"


@unique
class ServiceName(StrEnum):
    """Canonical service registry identifiers."""

    CONFIG = "CONFIG"
    EVENT_BUS = "EVENT_BUS"
    WEBSOCKET = "WEBSOCKET"
    TICK_STORE = "TICK_STORE"
    CHAIN_INGESTION = "CHAIN_INGESTION"
    VALIDATION = "VALIDATION"
    CACHE = "CACHE"
    TIMESCALE = "TIMESCALE"
    POSTGRES = "POSTGRES"
    REPLAY = "REPLAY"
    STRUCTURE = "STRUCTURE"
    OPTIONS_INTELLIGENCE = "OPTIONS_INTELLIGENCE"
    REGIME = "REGIME"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    ORCHESTRATOR = "ORCHESTRATOR"
    MONITORING = "MONITORING"