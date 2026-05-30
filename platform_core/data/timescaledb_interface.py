# FILE: platform_core/data/timescaledb_interface.py
"""
platform_core.data.timescaledb_interface
═══════════════════════════════════════════════════════════════════════
Production-grade async TimescaleDB interface foundation for Indian Index
Futures & Options market data workflows.

This module extends PostgreSQL-oriented storage contracts with hypertable,
compression, retention, and continuous aggregate lifecycle support while
remaining replay-safe and async-ready.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from platform_core.data.postgresql_interface import BulkCopySpec
from platform_core.data.postgresql_interface import InMemoryPostgreSQLPool
from platform_core.data.postgresql_interface import PostgreSQLConfig
from platform_core.data.postgresql_interface import PostgreSQLConnectionError
from platform_core.data.postgresql_interface import PostgreSQLInterface
from platform_core.data.postgresql_interface import PostgreSQLPool
from platform_core.data.postgresql_interface import PostgreSQLQueryError
from platform_core.data.postgresql_interface import PostgreSQLRepository
from platform_core.data.postgresql_interface import PostgreSQLTransaction
from platform_core.data.postgresql_interface import SQLStatement
from platform_core.data.postgresql_interface import SqlRow


class TimescaleDBError(RuntimeError):
    """Base exception for TimescaleDB interface failures."""


class TimescaleDBConfigurationError(TimescaleDBError):
    """Raised when TimescaleDB-specific configuration is invalid."""


class TimescaleDBOperationError(TimescaleDBError):
    """Raised when a TimescaleDB lifecycle operation fails."""


@dataclass(frozen=True, slots=True)
class TimescaleDBConfig:
    """Operational configuration for TimescaleDB administration and writes."""

    postgresql: PostgreSQLConfig = PostgreSQLConfig()
    ensure_extension_on_startup: bool = True
    default_chunk_interval: str = "1 day"
    enable_compression: bool = True
    compress_after_interval: str | None = "7 days"
    retention_period: str | None = None
    reorder_index_name: str | None = None
    continuous_aggregate_refresh_interval: str = "5 minutes"

    def __post_init__(self) -> None:
        if not self.default_chunk_interval.strip():
            raise TimescaleDBConfigurationError("default_chunk_interval must be non-empty")
        if self.compress_after_interval is not None and not self.compress_after_interval.strip():
            raise TimescaleDBConfigurationError("compress_after_interval must be non-empty when provided")
        if self.retention_period is not None and not self.retention_period.strip():
            raise TimescaleDBConfigurationError("retention_period must be non-empty when provided")
        if not self.continuous_aggregate_refresh_interval.strip():
            raise TimescaleDBConfigurationError("continuous_aggregate_refresh_interval must be non-empty")


@dataclass(frozen=True, slots=True)
class HypertableSpec:
    """Specification for creating or verifying a hypertable."""

    table_name: str
    time_column: str
    partition_column: str | None = None
    chunk_interval: str | None = None
    migrate_existing_data: bool = True
    if_not_exists: bool = True

    def __post_init__(self) -> None:
        if not self.table_name.strip():
            raise ValueError("table_name must be non-empty")
        if not self.time_column.strip():
            raise ValueError("time_column must be non-empty")
        if self.partition_column is not None and not self.partition_column.strip():
            raise ValueError("partition_column must be non-empty when provided")
        if self.chunk_interval is not None and not self.chunk_interval.strip():
            raise ValueError("chunk_interval must be non-empty when provided")


@dataclass(frozen=True, slots=True)
class CompressionPolicySpec:
    """Specification for TimescaleDB compression policy application."""

    table_name: str
    compress_after_interval: str
    segment_by_columns: tuple[str, ...] = ()
    order_by_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.table_name.strip():
            raise ValueError("table_name must be non-empty")
        if not self.compress_after_interval.strip():
            raise ValueError("compress_after_interval must be non-empty")


@dataclass(frozen=True, slots=True)
class RetentionPolicySpec:
    """Specification for TimescaleDB retention policy application."""

    object_name: str
    drop_after_interval: str
    if_exists: bool = True

    def __post_init__(self) -> None:
        if not self.object_name.strip():
            raise ValueError("object_name must be non-empty")
        if not self.drop_after_interval.strip():
            raise ValueError("drop_after_interval must be non-empty")


@dataclass(frozen=True, slots=True)
class ContinuousAggregateSpec:
    """Specification for creating a continuous aggregate."""

    view_name: str
    source_table_name: str
    bucket_interval: str
    bucket_column: str
    select_sql: str
    refresh_start_offset: str
    refresh_end_offset: str
    schedule_interval: str
    with_no_data: bool = False

    def __post_init__(self) -> None:
        required_fields = {
            "view_name": self.view_name,
            "source_table_name": self.source_table_name,
            "bucket_interval": self.bucket_interval,
            "bucket_column": self.bucket_column,
            "select_sql": self.select_sql,
            "refresh_start_offset": self.refresh_start_offset,
            "refresh_end_offset": self.refresh_end_offset,
            "schedule_interval": self.schedule_interval,
        }
        for field_name, field_value in required_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")


@dataclass(frozen=True, slots=True)
class TimescaleDBObjectStatus:
    """Typed status snapshot for a TimescaleDB object."""

    object_name: str
    exists: bool
    object_type: str
    observed_timestamp: datetime


class TimescaleDBInterface:
    """TimescaleDB lifecycle and persistence facade built on PostgreSQLInterface."""

    def __init__(self, *, pool: PostgreSQLPool, config: TimescaleDBConfig) -> None:
        self._config = config
        self._postgresql = PostgreSQLInterface(pool=pool, config=config.postgresql)
        self._repository = PostgreSQLRepository(interface=self._postgresql)

    @property
    def postgresql(self) -> PostgreSQLInterface:
        return self._postgresql

    @property
    def repository(self) -> PostgreSQLRepository:
        return self._repository

    async def initialize(self) -> None:
        if not self._config.ensure_extension_on_startup:
            return
        await self.execute_admin(
            SQLStatement(sql="CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")
        )

    async def close(self) -> None:
        await self._postgresql.close()

    async def execute_admin(self, statement: SQLStatement) -> str:
        try:
            return await self._postgresql.execute(statement)
        except PostgreSQLQueryError as exc:
            raise TimescaleDBOperationError("TimescaleDB admin operation failed") from exc

    async def fetch_admin(self, statement: SQLStatement) -> Sequence[SqlRow]:
        try:
            return await self._postgresql.fetch(statement)
        except PostgreSQLQueryError as exc:
            raise TimescaleDBOperationError("TimescaleDB admin query failed") from exc

    async def create_hypertable(self, spec: HypertableSpec) -> None:
        chunk_interval = spec.chunk_interval or self._config.default_chunk_interval
        positional_parts = [
            _sql_literal(spec.table_name),
            _sql_literal(spec.time_column),
        ]
        named_parts: list[str] = []
        if spec.partition_column is not None:
            named_parts.append(f"partitioning_column => {_sql_literal(spec.partition_column)}")
        named_parts.append(f"chunk_time_interval => INTERVAL {_sql_literal(chunk_interval)}")
        named_parts.append(f"migrate_data => {'TRUE' if spec.migrate_existing_data else 'FALSE'}")
        named_parts.append(f"if_not_exists => {'TRUE' if spec.if_not_exists else 'FALSE'}")
        sql = f"SELECT create_hypertable({', '.join(positional_parts + named_parts)})"
        await self.execute_admin(SQLStatement(sql=sql))

    async def enable_compression(self, spec: CompressionPolicySpec) -> None:
        options: list[str] = ["timescaledb.compress"]
        if spec.segment_by_columns:
            segment_by = ",".join(spec.segment_by_columns)
            options.append(f"timescaledb.compress_segmentby = '{segment_by}'")
        if spec.order_by_columns:
            order_by = ",".join(spec.order_by_columns)
            options.append(f"timescaledb.compress_orderby = '{order_by}'")
        sql = f"ALTER TABLE {spec.table_name} SET ({', '.join(options)})"
        await self.execute_admin(SQLStatement(sql=sql))
        policy_sql = (
            "SELECT add_compression_policy("  # noqa: S608
            f"{_sql_literal(spec.table_name)}, INTERVAL {_sql_literal(spec.compress_after_interval)}"
            ")"
        )
        await self.execute_admin(SQLStatement(sql=policy_sql))

    async def apply_retention_policy(self, spec: RetentionPolicySpec) -> None:
        sql = (
            "SELECT add_retention_policy("  # noqa: S608
            f"{_sql_literal(spec.object_name)}, INTERVAL {_sql_literal(spec.drop_after_interval)}"
            ")"
        )
        await self.execute_admin(SQLStatement(sql=sql))

    async def create_continuous_aggregate(self, spec: ContinuousAggregateSpec) -> None:
        with_no_data_clause = " WITH NO DATA" if spec.with_no_data else ""
        sql = (
            f"CREATE MATERIALIZED VIEW IF NOT EXISTS {spec.view_name} "
            "WITH (timescaledb.continuous) AS "
            f"{spec.select_sql}{with_no_data_clause}"
        )
        await self.execute_admin(SQLStatement(sql=sql))
        policy_sql = (
            "SELECT add_continuous_aggregate_policy("  # noqa: S608
            f"{_sql_literal(spec.view_name)}, "
            f"start_offset => INTERVAL {_sql_literal(spec.refresh_start_offset)}, "
            f"end_offset => INTERVAL {_sql_literal(spec.refresh_end_offset)}, "
            f"schedule_interval => INTERVAL {_sql_literal(spec.schedule_interval)}"
            ")"
        )
        await self.execute_admin(SQLStatement(sql=policy_sql))

    async def refresh_continuous_aggregate(
        self,
        *,
        view_name: str,
        window_start: datetime,
        window_end: datetime,
    ) -> None:
        start_utc = _ensure_utc(window_start)
        end_utc = _ensure_utc(window_end)
        sql = (
            "CALL refresh_continuous_aggregate("  # noqa: S608
            f"{_sql_literal(view_name)}, "
            f"TIMESTAMPTZ {_sql_literal(start_utc.isoformat())}, "
            f"TIMESTAMPTZ {_sql_literal(end_utc.isoformat())}"
            ")"
        )
        await self.execute_admin(SQLStatement(sql=sql))

    async def append_market_rows(self, *, table_name: str, columns: Sequence[str], records: Sequence[tuple[Any, ...]]) -> None:
        await self._repository.append_rows(table_name=table_name, columns=columns, records=records)

    async def transaction(self) -> PostgreSQLTransaction:
        return await self._postgresql.transaction()

    async def get_object_status(self, *, object_name: str, object_type: str) -> TimescaleDBObjectStatus:
        object_type_normalized = object_type.strip().lower()
        if object_type_normalized not in {"table", "materialized_view", "view"}:
            raise ValueError("object_type must be one of: table, materialized_view, view")
        if object_type_normalized == "table":
            sql = "SELECT to_regclass($1) AS object_name"
        else:
            sql = (
                "SELECT c.relname AS object_name "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = $1 AND c.relkind = $2"
            )
        parameters: tuple[Any, ...]
        if object_type_normalized == "materialized_view":
            parameters = (object_name, "m")
        elif object_type_normalized == "view":
            parameters = (object_name, "v")
        else:
            parameters = (object_name,)
        row = await self._postgresql.fetchrow(SQLStatement(sql=sql, parameters=parameters))
        exists = False
        if row is not None:
            if object_type_normalized == "table":
                exists = row.get("object_name") is not None
            else:
                exists = row.get("object_name") == object_name
        return TimescaleDBObjectStatus(
            object_name=object_name,
            exists=exists,
            object_type=object_type_normalized,
            observed_timestamp=_utc_now(),
        )

    async def bootstrap_market_data_table(
        self,
        *,
        create_table_sql: str,
        hypertable_spec: HypertableSpec,
        compression_spec: CompressionPolicySpec | None = None,
        retention_spec: RetentionPolicySpec | None = None,
    ) -> None:
        async with await self.transaction() as tx:
            await tx.execute(SQLStatement(sql=create_table_sql))
            await tx.execute(SQLStatement(sql=_build_create_hypertable_sql(hypertable_spec, self._config.default_chunk_interval)))
            if self._config.enable_compression and compression_spec is not None:
                await tx.execute(SQLStatement(sql=_build_alter_compression_sql(compression_spec)))
                await tx.execute(SQLStatement(sql=_build_add_compression_policy_sql(compression_spec)))
            if retention_spec is not None:
                await tx.execute(SQLStatement(sql=_build_add_retention_policy_sql(retention_spec)))


class InMemoryTimescaleDBInterface(TimescaleDBInterface):
    """Deterministic in-memory TimescaleDB interface for validation and replay."""

    def __init__(self, *, config: TimescaleDBConfig) -> None:
        super().__init__(pool=InMemoryPostgreSQLPool(), config=config)

    @property
    def memory_pool(self) -> InMemoryPostgreSQLPool:
        pool = self._postgresql._pool
        if not isinstance(pool, InMemoryPostgreSQLPool):
            raise TimescaleDBOperationError("underlying pool is not in-memory")
        return pool



def _build_create_hypertable_sql(spec: HypertableSpec, default_chunk_interval: str) -> str:
    chunk_interval = spec.chunk_interval or default_chunk_interval
    positional_parts = [
        _sql_literal(spec.table_name),
        _sql_literal(spec.time_column),
    ]
    named_parts: list[str] = []
    if spec.partition_column is not None:
        named_parts.append(f"partitioning_column => {_sql_literal(spec.partition_column)}")
    named_parts.append(f"chunk_time_interval => INTERVAL {_sql_literal(chunk_interval)}")
    named_parts.append(f"migrate_data => {'TRUE' if spec.migrate_existing_data else 'FALSE'}")
    named_parts.append(f"if_not_exists => {'TRUE' if spec.if_not_exists else 'FALSE'}")
    return f"SELECT create_hypertable({', '.join(positional_parts + named_parts)})"



def _build_alter_compression_sql(spec: CompressionPolicySpec) -> str:
    options: list[str] = ["timescaledb.compress"]
    if spec.segment_by_columns:
        options.append(f"timescaledb.compress_segmentby = '{','.join(spec.segment_by_columns)}'")
    if spec.order_by_columns:
        options.append(f"timescaledb.compress_orderby = '{','.join(spec.order_by_columns)}'")
    return f"ALTER TABLE {spec.table_name} SET ({', '.join(options)})"



def _build_add_compression_policy_sql(spec: CompressionPolicySpec) -> str:
    return (
        "SELECT add_compression_policy("  # noqa: S608
        f"{_sql_literal(spec.table_name)}, INTERVAL {_sql_literal(spec.compress_after_interval)}"
        ")"
    )



def _build_add_retention_policy_sql(spec: RetentionPolicySpec) -> str:
    return (
        "SELECT add_retention_policy("  # noqa: S608
        f"{_sql_literal(spec.object_name)}, INTERVAL {_sql_literal(spec.drop_after_interval)}"
        ")"
    )



def _sql_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "CompressionPolicySpec",
    "ContinuousAggregateSpec",
    "HypertableSpec",
    "InMemoryTimescaleDBInterface",
    "RetentionPolicySpec",
    "TimescaleDBConfig",
    "TimescaleDBConfigurationError",
    "TimescaleDBObjectStatus",
    "TimescaleDBError",
    "TimescaleDBInterface",
    "TimescaleDBOperationError",
]
