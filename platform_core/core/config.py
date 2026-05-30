# FILE: platform_core/core/config.py
"""
platform_core.core.config
═══════════════════════════════════════════════════════════════════════
Production-grade configuration system for the institutional derivatives
intelligence and automated execution platform specialized for Indian Index
Futures & Options markets.

Design guarantees preserved:
- Full package imports only.
- Pydantic-based typed configuration with validation.
- Async / streaming / replay compatible.
- Event-driven architecture continuity.
- Exchange-sensitive values remain configurable, never hard-coded as final.
- Deterministic, environment-aware loading.
- Safe serialization for logs, diagnostics, and event publication.
- No business logic; foundation layer only.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from platform_core.core.constants import (
    ARCHITECTURE_VERSION,
    DEFAULT_DLQ_DIR,
    DEFAULT_EVENT_BUS_QUEUE_SIZE,
    DEFAULT_LOG_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_REPLAY_CHUNK_SIZE,
    DEFAULT_REPLAY_SPEED_FACTOR,
    DEFAULT_STRIKE_STEPS,
    DEFAULT_LOT_SIZES,
    HEALTHCHECK_TIMEOUT_SEC,
    HEARTBEAT_INTERVAL_SEC,
    HOT_RETENTION_DAYS,
    LATENCY_CRITICAL_MS,
    LATENCY_WARNING_MS,
    MAX_ALLOWED_STALENESS_MS,
    MAX_CLOCK_SKEW_MS,
    MAX_EVENT_PAYLOAD_BYTES,
    MAX_ORDER_RETRY_ATTEMPTS,
    MIN_CHAIN_SNAPSHOT_QUALITY,
    MIN_EXECUTION_COOLDOWN_MS,
    POSTGRES_POOL_MAX_SIZE,
    POSTGRES_POOL_MIN_SIZE,
    REDIS_NAMESPACE_DEFAULT,
    REDIS_POOL_MAX_CONNECTIONS,
    RECONCILIATION_POLL_INTERVAL_SEC,
    REFERENCE_LOT_SIZES,
    REFERENCE_STRIKE_STEPS,
    REJECT_STORM_THRESHOLD,
    REPLAY_HEARTBEAT_INTERVAL_SEC,
    SERVICE_SHUTDOWN_TIMEOUT_SEC,
    TIMESCALE_FLUSH_INTERVAL_SEC,
    TIMESCALE_WRITE_BATCH_SIZE,
    WRITER_MAX_BUFFER,
    WRITER_MAX_RETRIES,
    WRITER_RETRY_BASE_DELAY_SEC,
)
from platform_core.core.enums import BarInterval, Environment, Exchange, ReplayMode, UnderlyingFamily
from platform_core.core.exceptions import ConfigurationError


class ConfigSection(BaseModel):
    """Base model for nested configuration sections."""

    model_config = {
        "extra": "forbid",
        "frozen": True,
        "use_enum_values": False,
    }


class RuntimeConfig(ConfigSection):
    """Top-level runtime controls."""

    environment: Environment = Environment.LOCAL
    service_name: str = "platform-core"
    architecture_version: str = ARCHITECTURE_VERSION
    debug: bool = False
    log_level: str = "INFO"
    heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC
    shutdown_timeout_sec: float = SERVICE_SHUTDOWN_TIMEOUT_SEC
    healthcheck_timeout_sec: float = HEALTHCHECK_TIMEOUT_SEC

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper().strip()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError(f"unsupported log level: {value}")
        return normalized


class PathConfig(ConfigSection):
    """Filesystem paths used by the platform."""

    output_dir: Path = DEFAULT_OUTPUT_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    dlq_dir: Path = DEFAULT_DLQ_DIR
    create_dirs_on_load: bool = True


class EventBusConfig(ConfigSection):
    """Event bus throughput and payload controls."""

    queue_size: int = DEFAULT_EVENT_BUS_QUEUE_SIZE
    max_payload_bytes: int = MAX_EVENT_PAYLOAD_BYTES
    strict_serialization: bool = True

    @field_validator("queue_size", "max_payload_bytes")
    @classmethod
    def positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be positive")
        return value


class ExchangeSessionConfig(ConfigSection):
    """Session-awareness controls for a given exchange."""

    exchange: Exchange
    enabled: bool = True
    allow_pre_open_processing: bool = False
    allow_post_close_processing: bool = True
    enforce_session_boundaries: bool = True
    clock_skew_tolerance_ms: int = MAX_CLOCK_SKEW_MS

    @field_validator("clock_skew_tolerance_ms")
    @classmethod
    def validate_clock_skew(cls, value: int) -> int:
        if value < 0:
            raise ValueError("clock_skew_tolerance_ms must be non-negative")
        return value


class MarketDefaultsConfig(ConfigSection):
    """
    Market-sensitive defaults.

    These values are reference defaults and remain config-overridable to preserve
    continuity with exchange circular changes.
    """

    default_exchange: Exchange = Exchange.NSE
    enabled_underlyings: tuple[UnderlyingFamily, ...] = (
        UnderlyingFamily.NIFTY,
        UnderlyingFamily.BANKNIFTY,
        UnderlyingFamily.FINNIFTY,
        UnderlyingFamily.SENSEX,
    )
    reference_strike_steps: dict[UnderlyingFamily, int] = Field(
        default_factory=lambda: {
            key: int(value)
            for key, value in REFERENCE_STRIKE_STEPS.items()
        }
    )
    reference_lot_sizes: dict[UnderlyingFamily, int] = Field(
        default_factory=lambda: dict(REFERENCE_LOT_SIZES)
    )

    @field_validator("reference_strike_steps")
    @classmethod
    def validate_strike_steps(cls, value: dict[UnderlyingFamily, int]) -> dict[UnderlyingFamily, int]:
        for underlying, step in value.items():
            if step <= 0:
                raise ValueError(f"strike step for {underlying.value} must be positive")
        return value

    @field_validator("reference_lot_sizes")
    @classmethod
    def validate_lot_sizes(cls, value: dict[UnderlyingFamily, int]) -> dict[UnderlyingFamily, int]:
        for underlying, lot_size in value.items():
            if lot_size <= 0:
                raise ValueError(f"lot size for {underlying.value} must be positive")
        return value


class ValidationConfig(ConfigSection):
    """Data validation and feed integrity controls."""

    max_clock_skew_ms: int = MAX_CLOCK_SKEW_MS
    max_allowed_staleness_ms: int = MAX_ALLOWED_STALENESS_MS
    min_chain_snapshot_quality: float = float(MIN_CHAIN_SNAPSHOT_QUALITY)
    strict_reject_on_schema_failure: bool = True
    quarantine_invalid_payloads: bool = True

    @field_validator("max_clock_skew_ms", "max_allowed_staleness_ms")
    @classmethod
    def non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value

    @field_validator("min_chain_snapshot_quality")
    @classmethod
    def bounded_quality(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("min_chain_snapshot_quality must be in [0, 1]")
        return value


class RedisConfig(ConfigSection):
    """Redis connectivity and cache controls."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: SecretStr | None = None
    namespace: str = REDIS_NAMESPACE_DEFAULT
    pool_max_connections: int = REDIS_POOL_MAX_CONNECTIONS
    ssl: bool = False

    @field_validator("port", "db", "pool_max_connections")
    @classmethod
    def non_negative_ports(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value


class PostgresConfig(ConfigSection):
    """PostgreSQL connectivity for durable state and metadata."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "platform"
    user: str = "platform"
    password: SecretStr | None = None
    pool_min_size: int = POSTGRES_POOL_MIN_SIZE
    pool_max_size: int = POSTGRES_POOL_MAX_SIZE
    command_timeout_sec: float = 30.0
    ssl: bool = False

    @model_validator(mode="after")
    def validate_pool_bounds(self) -> "PostgresConfig":
        if self.port <= 0:
            raise ValueError("port must be positive")
        if self.pool_min_size <= 0 or self.pool_max_size <= 0:
            raise ValueError("pool sizes must be positive")
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size cannot exceed pool_max_size")
        if self.command_timeout_sec <= 0:
            raise ValueError("command_timeout_sec must be positive")
        return self


class TimescaleConfig(ConfigSection):
    """TimescaleDB write-path controls."""

    enabled: bool = True
    hypertable_schema: str = "public"
    tick_table: str = "market_ticks"
    option_chain_table: str = "option_chain_snapshots"
    write_batch_size: int = TIMESCALE_WRITE_BATCH_SIZE
    flush_interval_sec: float = TIMESCALE_FLUSH_INTERVAL_SEC
    writer_max_buffer: int = WRITER_MAX_BUFFER
    writer_max_retries: int = WRITER_MAX_RETRIES
    writer_retry_base_delay_sec: float = WRITER_RETRY_BASE_DELAY_SEC
    hot_retention_days: int = HOT_RETENTION_DAYS

    @model_validator(mode="after")
    def validate_write_path(self) -> "TimescaleConfig":
        if self.write_batch_size <= 0:
            raise ValueError("write_batch_size must be positive")
        if self.flush_interval_sec <= 0:
            raise ValueError("flush_interval_sec must be positive")
        if self.writer_max_buffer <= 0:
            raise ValueError("writer_max_buffer must be positive")
        if self.writer_max_retries < 0:
            raise ValueError("writer_max_retries must be non-negative")
        if self.writer_retry_base_delay_sec <= 0:
            raise ValueError("writer_retry_base_delay_sec must be positive")
        if self.hot_retention_days <= 0:
            raise ValueError("hot_retention_days must be positive")
        return self


class ReplayConfig(ConfigSection):
    """Replay-engine controls preserving deterministic causal execution."""

    enabled: bool = True
    mode: ReplayMode = ReplayMode.TICK_ACCURATE
    speed_factor: float = DEFAULT_REPLAY_SPEED_FACTOR
    chunk_size: int = DEFAULT_REPLAY_CHUNK_SIZE
    heartbeat_interval_sec: float = REPLAY_HEARTBEAT_INTERVAL_SEC
    deterministic_mode: bool = True
    finalized_bar_only: bool = True
    forbid_future_leakage: bool = True

    @model_validator(mode="after")
    def validate_replay(self) -> "ReplayConfig":
        if self.speed_factor < 0:
            raise ValueError("speed_factor must be non-negative")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self.heartbeat_interval_sec <= 0:
            raise ValueError("heartbeat_interval_sec must be positive")
        return self


class ExecutionSafetyConfig(ConfigSection):
    """Shared execution safeguards used by later execution-engine modules."""

    enabled: bool = True
    max_order_retry_attempts: int = MAX_ORDER_RETRY_ATTEMPTS
    min_execution_cooldown_ms: int = MIN_EXECUTION_COOLDOWN_MS
    reject_storm_threshold: int = REJECT_STORM_THRESHOLD
    reconciliation_poll_interval_sec: float = RECONCILIATION_POLL_INTERVAL_SEC
    latency_warning_ms: float = LATENCY_WARNING_MS
    latency_critical_ms: float = LATENCY_CRITICAL_MS

    @model_validator(mode="after")
    def validate_safety(self) -> "ExecutionSafetyConfig":
        if self.max_order_retry_attempts < 0:
            raise ValueError("max_order_retry_attempts must be non-negative")
        if self.min_execution_cooldown_ms < 0:
            raise ValueError("min_execution_cooldown_ms must be non-negative")
        if self.reject_storm_threshold <= 0:
            raise ValueError("reject_storm_threshold must be positive")
        if self.reconciliation_poll_interval_sec <= 0:
            raise ValueError("reconciliation_poll_interval_sec must be positive")
        if self.latency_warning_ms <= 0 or self.latency_critical_ms <= 0:
            raise ValueError("latency thresholds must be positive")
        if self.latency_warning_ms >= self.latency_critical_ms:
            raise ValueError("latency_warning_ms must be below latency_critical_ms")
        return self


class AnalyticsConfig(ConfigSection):
    """Shared analytics timing and interval defaults."""

    primary_signal_bar_interval: BarInterval = BarInterval.MIN_1
    secondary_signal_bar_interval: BarInterval = BarInterval.MIN_5
    allow_intrabar_partial_updates: bool = False
    finalized_bar_required: bool = True
    causal_only: bool = True


class PlatformConfig(BaseSettings):
    """Top-level settings object for the full platform."""

    model_config = SettingsConfigDict(
        env_prefix="PLATFORM_",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    paths: PathConfig = Field(default_factory=PathConfig)
    event_bus: EventBusConfig = Field(default_factory=EventBusConfig)
    market: MarketDefaultsConfig = Field(default_factory=MarketDefaultsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    timescale: TimescaleConfig = Field(default_factory=TimescaleConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    execution_safety: ExecutionSafetyConfig = Field(default_factory=ExecutionSafetyConfig)
    analytics: AnalyticsConfig = Field(default_factory=AnalyticsConfig)
    exchange_sessions: tuple[ExchangeSessionConfig, ...] = (
        ExchangeSessionConfig(exchange=Exchange.NSE),
        ExchangeSessionConfig(exchange=Exchange.BSE),
    )

    @model_validator(mode="after")
    def validate_cross_section_consistency(self) -> "PlatformConfig":
        enabled_exchanges = {session.exchange for session in self.exchange_sessions if session.enabled}
        if self.market.default_exchange not in enabled_exchanges:
            raise ValueError(
                "market.default_exchange must exist in enabled exchange_sessions"
            )
        if self.analytics.finalized_bar_required and not self.replay.finalized_bar_only:
            raise ValueError(
                "analytics.finalized_bar_required cannot be true when replay.finalized_bar_only is false"
            )
        if self.analytics.causal_only and not self.replay.forbid_future_leakage:
            raise ValueError(
                "analytics.causal_only requires replay.forbid_future_leakage to be true"
            )
        return self

    def ensure_directories(self) -> None:
        """Create configured directories when enabled."""
        if not self.paths.create_dirs_on_load:
            return
        self.paths.output_dir.mkdir(parents=True, exist_ok=True)
        self.paths.log_dir.mkdir(parents=True, exist_ok=True)
        self.paths.dlq_dir.mkdir(parents=True, exist_ok=True)

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-serializable configuration view."""
        data = self.model_dump(mode="json")
        if data.get("redis", {}).get("password") is not None:
            data["redis"]["password"] = "********"
        if data.get("postgres", {}).get("password") is not None:
            data["postgres"]["password"] = "********"
        return data


@lru_cache(maxsize=1)
def get_config() -> PlatformConfig:
    """
    Load and cache validated platform configuration.

    Raises:
        ConfigurationError: if validation or initialization fails.
    """
    try:
        config = PlatformConfig()
        config.ensure_directories()
        return config
    except Exception as exc:  # pragma: no cover - defensive boundary normalization
        raise ConfigurationError(
            "failed to initialize platform configuration",
            cause=exc,
        ) from exc


def reset_config_cache() -> None:
    """Clear the cached config instance for tests or controlled reloads."""
    get_config.cache_clear()


__all__ = [
    "AnalyticsConfig",
    "ConfigSection",
    "EventBusConfig",
    "ExchangeSessionConfig",
    "ExecutionSafetyConfig",
    "MarketDefaultsConfig",
    "PathConfig",
    "PlatformConfig",
    "PostgresConfig",
    "RedisConfig",
    "ReplayConfig",
    "RuntimeConfig",
    "TimescaleConfig",
    "ValidationConfig",
    "get_config",
    "reset_config_cache",
]
