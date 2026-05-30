# FILE: platform_core/data/postgresql_interface.py
"""
platform_core.data.postgresql_interface
═══════════════════════════════════════════════════════════════════════
Production-grade async PostgreSQL interface foundation for Indian Index
Futures & Options market data workflows.

This module is designed to be:
- async-first,
- batch-write compatible,
- repository-oriented,
- replay-compatible,
- event-driven compatible,
- dependency-clean,
- storage-backend agnostic,
- PostgreSQL and TimescaleDB adapter ready.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)

SqlParameters = Sequence[Any]
SqlRow = Mapping[str, Any]


class PostgreSQLException(RuntimeError):
    """Base exception for PostgreSQL interface failures."""


class PostgreSQLConnectionError(PostgreSQLException):
    """Raised when a PostgreSQL connection or pool operation fails."""


class PostgreSQLQueryError(PostgreSQLException):
    """Raised when a SQL statement execution fails."""


class PostgreSQLTransactionError(PostgreSQLException):
    """Raised when a transaction fails or is misused."""


@dataclass(frozen=True, slots=True)
class PostgreSQLConfig:
    """Operational configuration for PostgreSQL access."""

    host: str = "localhost"
    port: int = 5432
    database: str = "platform_core"
    user: str = "postgres"
    password: str = "postgres"
    min_pool_size: int = 1
    max_pool_size: int = 10
    command_timeout_seconds: float = 5.0
    statement_cache_size: int = 128
    connect_timeout_seconds: float = 5.0
    use_copy_for_bulk_writes: bool = True

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("host must be non-empty")
        if self.port <= 0:
            raise ValueError("port must be positive")
        if not self.database.strip():
            raise ValueError("database must be non-empty")
        if not self.user.strip():
            raise ValueError("user must be non-empty")
        if self.min_pool_size <= 0 or self.max_pool_size <= 0:
            raise ValueError("pool sizes must be positive")
        if self.min_pool_size > self.max_pool_size:
            raise ValueError("min_pool_size cannot exceed max_pool_size")
        if self.command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if self.statement_cache_size < 0:
            raise ValueError("statement_cache_size cannot be negative")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class PostgreSQLStats:
    """Operational metrics for PostgreSQL repository usage."""

    execute_calls: int = 0
    executemany_calls: int = 0
    fetch_calls: int = 0
    fetchrow_calls: int = 0
    copy_calls: int = 0
    transaction_count: int = 0
    last_error_timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class SQLStatement:
    """Typed SQL statement container."""

    sql: str
    parameters: SqlParameters = ()

    def __post_init__(self) -> None:
        if not self.sql.strip():
            raise ValueError("sql must be non-empty")


@dataclass(frozen=True, slots=True)
class BulkCopySpec:
    """Bulk copy specification for COPY-compatible backends."""

    table_name: str
    columns: tuple[str, ...]
    records: tuple[tuple[Any, ...], ...]

    def __post_init__(self) -> None:
        if not self.table_name.strip():
            raise ValueError("table_name must be non-empty")
        if not self.columns:
            raise ValueError("columns cannot be empty")
        if not self.records:
            raise ValueError("records cannot be empty")
        width = len(self.columns)
        for record in self.records:
            if len(record) != width:
                raise ValueError("each record must match the number of columns")


class PostgreSQLConnection(ABC):
    """Abstract logical PostgreSQL connection."""

    @abstractmethod
    async def execute(self, sql: str, *parameters: Any) -> str:
        """Execute a statement and return backend status."""

    @abstractmethod
    async def executemany(self, sql: str, parameter_sets: Sequence[SqlParameters]) -> None:
        """Execute many parameterized statements."""

    @abstractmethod
    async def fetch(self, sql: str, *parameters: Any) -> Sequence[SqlRow]:
        """Fetch multiple rows."""

    @abstractmethod
    async def fetchrow(self, sql: str, *parameters: Any) -> SqlRow | None:
        """Fetch a single row."""

    @abstractmethod
    async def copy_records_to_table(self, table_name: str, *, records: Sequence[tuple[Any, ...]], columns: Sequence[str]) -> None:
        """Bulk load records into a table."""


class PostgreSQLPool(ABC):
    """Abstract connection pool boundary."""

    @abstractmethod
    async def acquire(self) -> PostgreSQLConnection:
        """Acquire a logical connection from the pool."""

    @abstractmethod
    async def release(self, connection: PostgreSQLConnection) -> None:
        """Release a logical connection back to the pool."""

    @abstractmethod
    async def close(self) -> None:
        """Close the pool."""


class InMemoryPostgreSQLConnection(PostgreSQLConnection):
    """Deterministic in-memory connection for tests and replay-safe validation."""

    def __init__(self, storage: MutableSQLStorage) -> None:
        self._storage = storage

    async def execute(self, sql: str, *parameters: Any) -> str:
        self._storage.executed_statements.append((sql, tuple(parameters)))
        return "EXECUTED"

    async def executemany(self, sql: str, parameter_sets: Sequence[SqlParameters]) -> None:
        for parameter_set in parameter_sets:
            self._storage.executed_statements.append((sql, tuple(parameter_set)))

    async def fetch(self, sql: str, *parameters: Any) -> Sequence[SqlRow]:
        self._storage.executed_statements.append((sql, tuple(parameters)))
        return tuple(self._storage.fetch_results.get((sql, tuple(parameters)), ()))

    async def fetchrow(self, sql: str, *parameters: Any) -> SqlRow | None:
        rows = await self.fetch(sql, *parameters)
        return rows[0] if rows else None

    async def copy_records_to_table(self, table_name: str, *, records: Sequence[tuple[Any, ...]], columns: Sequence[str]) -> None:
        self._storage.copy_operations.append((table_name, tuple(columns), tuple(records)))


@dataclass
class MutableSQLStorage:
    """In-memory mutable storage for the deterministic PostgreSQL backend."""

    executed_statements: list[tuple[str, tuple[Any, ...]]]
    copy_operations: list[tuple[str, tuple[str, ...], tuple[tuple[Any, ...], ...]]]
    fetch_results: dict[tuple[str, tuple[Any, ...]], tuple[SqlRow, ...]]


class InMemoryPostgreSQLPool(PostgreSQLPool):
    """Deterministic in-memory pool implementation."""

    def __init__(self) -> None:
        self._storage = MutableSQLStorage(executed_statements=[], copy_operations=[], fetch_results={})
        self._connection = InMemoryPostgreSQLConnection(self._storage)
        self._closed = False

    async def acquire(self) -> PostgreSQLConnection:
        if self._closed:
            raise PostgreSQLConnectionError("in-memory PostgreSQL pool is closed")
        return self._connection

    async def release(self, connection: PostgreSQLConnection) -> None:
        if connection is not self._connection:
            raise PostgreSQLConnectionError("connection does not belong to this pool")

    async def close(self) -> None:
        self._closed = True

    @property
    def storage(self) -> MutableSQLStorage:
        return self._storage


class PostgreSQLInterface:
    """Async PostgreSQL repository facade with explicit transaction boundaries."""

    def __init__(self, *, pool: PostgreSQLPool, config: PostgreSQLConfig) -> None:
        self._pool = pool
        self._config = config
        self._stats = PostgreSQLStats()

    @property
    def stats(self) -> PostgreSQLStats:
        return self._stats

    async def execute(self, statement: SQLStatement) -> str:
        connection = await self._acquire_connection()
        try:
            result = await asyncio.wait_for(
                connection.execute(statement.sql, *statement.parameters),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise PostgreSQLQueryError("failed to execute SQL statement") from exc
        finally:
            await self._safe_release(connection)
        self._stats = PostgreSQLStats(
            execute_calls=self._stats.execute_calls + 1,
            executemany_calls=self._stats.executemany_calls,
            fetch_calls=self._stats.fetch_calls,
            fetchrow_calls=self._stats.fetchrow_calls,
            copy_calls=self._stats.copy_calls,
            transaction_count=self._stats.transaction_count,
            last_error_timestamp=self._stats.last_error_timestamp,
        )
        return result

    async def executemany(self, statement: SQLStatement, parameter_sets: Sequence[SqlParameters]) -> None:
        connection = await self._acquire_connection()
        try:
            await asyncio.wait_for(
                connection.executemany(statement.sql, parameter_sets),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise PostgreSQLQueryError("failed to execute many SQL statements") from exc
        finally:
            await self._safe_release(connection)
        self._stats = PostgreSQLStats(
            execute_calls=self._stats.execute_calls,
            executemany_calls=self._stats.executemany_calls + 1,
            fetch_calls=self._stats.fetch_calls,
            fetchrow_calls=self._stats.fetchrow_calls,
            copy_calls=self._stats.copy_calls,
            transaction_count=self._stats.transaction_count,
            last_error_timestamp=self._stats.last_error_timestamp,
        )

    async def fetch(self, statement: SQLStatement) -> Sequence[SqlRow]:
        connection = await self._acquire_connection()
        try:
            rows = await asyncio.wait_for(
                connection.fetch(statement.sql, *statement.parameters),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise PostgreSQLQueryError("failed to fetch SQL rows") from exc
        finally:
            await self._safe_release(connection)
        self._stats = PostgreSQLStats(
            execute_calls=self._stats.execute_calls,
            executemany_calls=self._stats.executemany_calls,
            fetch_calls=self._stats.fetch_calls + 1,
            fetchrow_calls=self._stats.fetchrow_calls,
            copy_calls=self._stats.copy_calls,
            transaction_count=self._stats.transaction_count,
            last_error_timestamp=self._stats.last_error_timestamp,
        )
        return rows

    async def fetchrow(self, statement: SQLStatement) -> SqlRow | None:
        connection = await self._acquire_connection()
        try:
            row = await asyncio.wait_for(
                connection.fetchrow(statement.sql, *statement.parameters),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise PostgreSQLQueryError("failed to fetch SQL row") from exc
        finally:
            await self._safe_release(connection)
        self._stats = PostgreSQLStats(
            execute_calls=self._stats.execute_calls,
            executemany_calls=self._stats.executemany_calls,
            fetch_calls=self._stats.fetch_calls,
            fetchrow_calls=self._stats.fetchrow_calls + 1,
            copy_calls=self._stats.copy_calls,
            transaction_count=self._stats.transaction_count,
            last_error_timestamp=self._stats.last_error_timestamp,
        )
        return row

    async def bulk_copy(self, spec: BulkCopySpec) -> None:
        connection = await self._acquire_connection()
        try:
            await asyncio.wait_for(
                connection.copy_records_to_table(spec.table_name, records=spec.records, columns=spec.columns),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise PostgreSQLQueryError("failed to perform bulk copy") from exc
        finally:
            await self._safe_release(connection)
        self._stats = PostgreSQLStats(
            execute_calls=self._stats.execute_calls,
            executemany_calls=self._stats.executemany_calls,
            fetch_calls=self._stats.fetch_calls,
            fetchrow_calls=self._stats.fetchrow_calls,
            copy_calls=self._stats.copy_calls + 1,
            transaction_count=self._stats.transaction_count,
            last_error_timestamp=self._stats.last_error_timestamp,
        )

    async def transaction(self) -> "PostgreSQLTransaction":
        connection = await self._acquire_connection()
        return PostgreSQLTransaction(interface=self, connection=connection)

    async def close(self) -> None:
        await self._pool.close()

    async def _acquire_connection(self) -> PostgreSQLConnection:
        try:
            return await asyncio.wait_for(self._pool.acquire(), timeout=self._config.connect_timeout_seconds)
        except Exception as exc:
            self._mark_error()
            raise PostgreSQLConnectionError("failed to acquire PostgreSQL connection") from exc

    async def _safe_release(self, connection: PostgreSQLConnection) -> None:
        with contextlib.suppress(Exception):
            await self._pool.release(connection)

    def _mark_error(self) -> None:
        self._stats = PostgreSQLStats(
            execute_calls=self._stats.execute_calls,
            executemany_calls=self._stats.executemany_calls,
            fetch_calls=self._stats.fetch_calls,
            fetchrow_calls=self._stats.fetchrow_calls,
            copy_calls=self._stats.copy_calls,
            transaction_count=self._stats.transaction_count,
            last_error_timestamp=_utc_now(),
        )


class PostgreSQLTransaction:
    """Explicit async transaction scope bound to one acquired connection."""

    def __init__(self, *, interface: PostgreSQLInterface, connection: PostgreSQLConnection) -> None:
        self._interface = interface
        self._connection = connection
        self._closed = False
        self._completed = False

    async def __aenter__(self) -> "PostgreSQLTransaction":
        await self._connection.execute("BEGIN")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc is None and not self._completed:
                await self.commit()
            elif exc is not None and not self._completed:
                await self.rollback()
        finally:
            await self._interface._safe_release(self._connection)
            self._closed = True

    async def execute(self, statement: SQLStatement) -> str:
        self._ensure_open()
        try:
            return await self._connection.execute(statement.sql, *statement.parameters)
        except Exception as exc:
            raise PostgreSQLTransactionError("transaction execute failed") from exc

    async def executemany(self, statement: SQLStatement, parameter_sets: Sequence[SqlParameters]) -> None:
        self._ensure_open()
        try:
            await self._connection.executemany(statement.sql, parameter_sets)
        except Exception as exc:
            raise PostgreSQLTransactionError("transaction executemany failed") from exc

    async def fetch(self, statement: SQLStatement) -> Sequence[SqlRow]:
        self._ensure_open()
        try:
            return await self._connection.fetch(statement.sql, *statement.parameters)
        except Exception as exc:
            raise PostgreSQLTransactionError("transaction fetch failed") from exc

    async def fetchrow(self, statement: SQLStatement) -> SqlRow | None:
        self._ensure_open()
        try:
            return await self._connection.fetchrow(statement.sql, *statement.parameters)
        except Exception as exc:
            raise PostgreSQLTransactionError("transaction fetchrow failed") from exc

    async def bulk_copy(self, spec: BulkCopySpec) -> None:
        self._ensure_open()
        try:
            await self._connection.copy_records_to_table(spec.table_name, records=spec.records, columns=spec.columns)
        except Exception as exc:
            raise PostgreSQLTransactionError("transaction bulk copy failed") from exc

    async def commit(self) -> None:
        self._ensure_open()
        try:
            await self._connection.execute("COMMIT")
            self._completed = True
            self._interface._stats = PostgreSQLStats(
                execute_calls=self._interface._stats.execute_calls,
                executemany_calls=self._interface._stats.executemany_calls,
                fetch_calls=self._interface._stats.fetch_calls,
                fetchrow_calls=self._interface._stats.fetchrow_calls,
                copy_calls=self._interface._stats.copy_calls,
                transaction_count=self._interface._stats.transaction_count + 1,
                last_error_timestamp=self._interface._stats.last_error_timestamp,
            )
        except Exception as exc:
            raise PostgreSQLTransactionError("transaction commit failed") from exc

    async def rollback(self) -> None:
        if self._closed:
            return
        try:
            await self._connection.execute("ROLLBACK")
            self._completed = True
        except Exception as exc:
            raise PostgreSQLTransactionError("transaction rollback failed") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise PostgreSQLTransactionError("transaction is already closed")


class PostgreSQLRepository:
    """Higher-level repository helpers for append-only and query-oriented workflows."""

    def __init__(self, *, interface: PostgreSQLInterface) -> None:
        self._interface = interface

    async def append_rows(self, *, table_name: str, columns: Sequence[str], records: Sequence[tuple[Any, ...]]) -> None:
        if not table_name.strip():
            raise ValueError("table_name must be non-empty")
        if not columns:
            raise ValueError("columns cannot be empty")
        if not records:
            return
        if self._interface._config.use_copy_for_bulk_writes:
            await self._interface.bulk_copy(
                BulkCopySpec(
                    table_name=table_name,
                    columns=tuple(columns),
                    records=tuple(records),
                )
            )
            return
        placeholders = "(" + ",".join(f"${index + 1}" for index in range(len(columns))) + ")"
        sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES {placeholders}"
        await self._interface.executemany(SQLStatement(sql=sql), parameter_sets=tuple(records))

    async def fetch_all(self, statement: SQLStatement) -> Sequence[SqlRow]:
        return await self._interface.fetch(statement)

    async def fetch_one(self, statement: SQLStatement) -> SqlRow | None:
        return await self._interface.fetchrow(statement)

    async def stream_batches(
        self,
        *,
        statement_factory: callable[[int], SQLStatement],
        start_offset: int = 0,
        batch_size: int = 1_000,
    ) -> AsyncIterator[Sequence[SqlRow]]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        offset = start_offset
        while True:
            statement = statement_factory(offset)
            rows = await self._interface.fetch(statement)
            if not rows:
                return
            yield rows
            offset += batch_size



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "BulkCopySpec",
    "InMemoryPostgreSQLConnection",
    "InMemoryPostgreSQLPool",
    "MutableSQLStorage",
    "PostgreSQLConfig",
    "PostgreSQLConnection",
    "PostgreSQLConnectionError",
    "PostgreSQLInterface",
    "PostgreSQLPool",
    "PostgreSQLQueryError",
    "PostgreSQLRepository",
    "PostgreSQLException",
    "PostgreSQLStats",
    "PostgreSQLTransaction",
    "PostgreSQLTransactionError",
    "SQLStatement",
    "SqlParameters",
    "SqlRow",
]
