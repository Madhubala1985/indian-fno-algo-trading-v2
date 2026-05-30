# FILE: platform_core/data/validation_pipeline.py
"""
platform_core.data.validation_pipeline
═══════════════════════════════════════════════════════════════════════
Production-grade async validation pipeline for Indian Index Futures &
Options market data workflows.

This module provides deterministic, replay-safe validation for streaming and
batch market-data payloads. It performs schema checks, type coercion, field
rules, monotonic timestamp validation, anomaly flagging, and configurable
accept/reject routing without introducing future leakage.
"""
from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from enum import Enum
from typing import Any

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
ValidationPayload = Mapping[str, Any]
ValidatorCallable = Callable[["ValidationContext"], Awaitable["ValidationRuleResult"] | "ValidationRuleResult"]


class ValidationSeverity(str, Enum):
    """Severity level for validation findings."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ValidationDecision(str, Enum):
    """Pipeline decision after validation completes."""

    ACCEPT = "accept"
    ACCEPT_WITH_WARNINGS = "accept_with_warnings"
    REJECT = "reject"


class ValidationCategory(str, Enum):
    """Typed categories for validation findings."""

    SCHEMA = "schema"
    TYPE = "type"
    MISSING_FIELD = "missing_field"
    RANGE = "range"
    TEMPORAL = "temporal"
    MONOTONICITY = "monotonicity"
    ANOMALY = "anomaly"
    BUSINESS_RULE = "business_rule"
    NORMALIZATION = "normalization"


class ValidationPipelineError(RuntimeError):
    """Base exception for validation pipeline failures."""


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """A single validation finding emitted by a rule."""

    rule_name: str
    severity: ValidationSeverity
    category: ValidationCategory
    message: str
    field_name: str | None = None
    value: JsonValue | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "category": self.category.value,
            "message": self.message,
            "field_name": self.field_name,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class ValidationRuleResult:
    """Typed output of an individual validation rule."""

    findings: tuple[ValidationFinding, ...] = ()
    mutated_payload: dict[str, JsonValue] | None = None

    @property
    def has_errors(self) -> bool:
        return any(f.severity in {ValidationSeverity.ERROR, ValidationSeverity.CRITICAL} for f in self.findings)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Final pipeline result for one payload."""

    decision: ValidationDecision
    normalized_payload: dict[str, JsonValue]
    findings: tuple[ValidationFinding, ...]
    ingestion_timestamp: datetime
    source_timestamp: datetime | None
    sequence_key: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "decision": self.decision.value,
            "normalized_payload": self.normalized_payload,
            "findings": [finding.to_dict() for finding in self.findings],
            "ingestion_timestamp": self.ingestion_timestamp.isoformat(),
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
            "sequence_key": self.sequence_key,
        }


@dataclass(frozen=True, slots=True)
class ValidationFieldSpec:
    """Schema and constraint specification for one field."""

    name: str
    required: bool = True
    accepted_types: tuple[type[Any], ...] = ()
    allow_none: bool = False
    min_value: float | None = None
    max_value: float | None = None
    allowed_values: tuple[JsonValue, ...] = ()
    coerce_numeric: bool = False
    coerce_datetime: bool = False
    coerce_date: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("field spec name must be non-empty")
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("min_value cannot exceed max_value")


@dataclass(frozen=True, slots=True)
class ValidationSchema:
    """Schema definition for canonical payload validation."""

    schema_name: str
    fields: tuple[ValidationFieldSpec, ...]
    allow_unknown_fields: bool = True

    def __post_init__(self) -> None:
        if not self.schema_name.strip():
            raise ValueError("schema_name must be non-empty")
        field_names = [field_spec.name for field_spec in self.fields]
        if len(field_names) != len(set(field_names)):
            raise ValueError("schema field names must be unique")

    @property
    def field_map(self) -> dict[str, ValidationFieldSpec]:
        return {field_spec.name: field_spec for field_spec in self.fields}


@dataclass(frozen=True, slots=True)
class ValidationPipelineConfig:
    """Operational validation configuration."""

    reject_on_warning: bool = False
    max_findings: int = 128
    enforce_monotonic_timestamps: bool = True
    source_timestamp_field: str | None = "event_timestamp"
    sequence_key_fields: tuple[str, ...] = ()
    anomaly_window_size: int = 128
    anomaly_z_score_threshold: float = 8.0
    numeric_anomaly_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_findings <= 0:
            raise ValueError("max_findings must be positive")
        if self.anomaly_window_size <= 2:
            raise ValueError("anomaly_window_size must be greater than 2")
        if self.anomaly_z_score_threshold <= 0:
            raise ValueError("anomaly_z_score_threshold must be positive")


@dataclass(slots=True)
class ValidationContext:
    """Mutable validation context passed through the pipeline."""

    payload: dict[str, JsonValue]
    original_payload: Mapping[str, Any]
    schema: ValidationSchema
    config: ValidationPipelineConfig
    ingestion_timestamp: datetime
    source_timestamp: datetime | None
    sequence_key: str | None
    findings: list[ValidationFinding] = field(default_factory=list)

    def add_finding(
        self,
        *,
        rule_name: str,
        severity: ValidationSeverity,
        category: ValidationCategory,
        message: str,
        field_name: str | None = None,
        value: JsonValue | None = None,
    ) -> None:
        if len(self.findings) >= self.config.max_findings:
            return
        self.findings.append(
            ValidationFinding(
                rule_name=rule_name,
                severity=severity,
                category=category,
                message=message,
                field_name=field_name,
                value=value,
            )
        )


@dataclass(slots=True)
class ValidationStats:
    """Operational counters for the validation pipeline."""

    accepted_count: int = 0
    accepted_with_warnings_count: int = 0
    rejected_count: int = 0
    processed_count: int = 0
    anomaly_warning_count: int = 0


class ValidationRule:
    """Named validation rule wrapper."""

    def __init__(self, *, name: str, validator: ValidatorCallable) -> None:
        if not name.strip():
            raise ValueError("rule name must be non-empty")
        self._name = name.strip()
        self._validator = validator

    @property
    def name(self) -> str:
        return self._name

    async def run(self, context: ValidationContext) -> ValidationRuleResult:
        result = self._validator(context)
        if asyncio.iscoroutine(result):
            awaited = await result
        else:
            awaited = result
        if not isinstance(awaited, ValidationRuleResult):
            raise ValidationPipelineError(f"rule {self._name} returned invalid result type")
        return awaited


class ValidationPipeline:
    """Deterministic multi-stage validation pipeline."""

    def __init__(
        self,
        *,
        schema: ValidationSchema,
        config: ValidationPipelineConfig,
        custom_rules: Sequence[ValidationRule] = (),
    ) -> None:
        self._schema = schema
        self._config = config
        self._custom_rules = tuple(custom_rules)
        self._stats = ValidationStats()
        self._last_timestamp_by_sequence: dict[str, datetime] = {}
        self._numeric_history: dict[str, deque[float]] = {
            field_name: deque(maxlen=config.anomaly_window_size) for field_name in config.numeric_anomaly_fields
        }

    @property
    def stats(self) -> ValidationStats:
        return self._stats

    async def validate(self, payload: ValidationPayload) -> ValidationResult:
        ingestion_timestamp = _utc_now()
        normalized_payload = _json_safe_mapping(payload)
        source_timestamp = _extract_timestamp(normalized_payload, self._config.source_timestamp_field)
        sequence_key = _build_sequence_key(normalized_payload, self._config.sequence_key_fields)
        context = ValidationContext(
            payload=normalized_payload,
            original_payload=payload,
            schema=self._schema,
            config=self._config,
            ingestion_timestamp=ingestion_timestamp,
            source_timestamp=source_timestamp,
            sequence_key=sequence_key,
        )
        rules = self._build_rules()
        for rule in rules:
            result = await rule.run(context)
            for finding in result.findings:
                context.add_finding(
                    rule_name=finding.rule_name,
                    severity=finding.severity,
                    category=finding.category,
                    message=finding.message,
                    field_name=finding.field_name,
                    value=finding.value,
                )
            if result.mutated_payload is not None:
                context.payload = dict(result.mutated_payload)

        decision = self._decide(context.findings)
        if decision != ValidationDecision.REJECT:
            self._update_monotonic_state(context)
            self._update_anomaly_state(context)
        self._update_stats(decision, context.findings)
        return ValidationResult(
            decision=decision,
            normalized_payload=context.payload,
            findings=tuple(context.findings),
            ingestion_timestamp=ingestion_timestamp,
            source_timestamp=context.source_timestamp,
            sequence_key=sequence_key,
        )

    def _build_rules(self) -> tuple[ValidationRule, ...]:
        builtin_rules = (
            ValidationRule(name="schema_and_type_validation", validator=self._validate_schema_and_types),
            ValidationRule(name="unknown_field_validation", validator=self._validate_unknown_fields),
            ValidationRule(name="timestamp_monotonicity_validation", validator=self._validate_timestamp_monotonicity),
            ValidationRule(name="numeric_anomaly_validation", validator=self._validate_numeric_anomalies),
        )
        return builtin_rules + self._custom_rules

    def _decide(self, findings: Sequence[ValidationFinding]) -> ValidationDecision:
        has_error = any(f.severity in {ValidationSeverity.ERROR, ValidationSeverity.CRITICAL} for f in findings)
        has_warning = any(f.severity == ValidationSeverity.WARNING for f in findings)
        if has_error:
            return ValidationDecision.REJECT
        if has_warning:
            if self._config.reject_on_warning:
                return ValidationDecision.REJECT
            return ValidationDecision.ACCEPT_WITH_WARNINGS
        return ValidationDecision.ACCEPT

    def _update_stats(self, decision: ValidationDecision, findings: Sequence[ValidationFinding]) -> None:
        self._stats.processed_count += 1
        if decision == ValidationDecision.ACCEPT:
            self._stats.accepted_count += 1
        elif decision == ValidationDecision.ACCEPT_WITH_WARNINGS:
            self._stats.accepted_with_warnings_count += 1
        else:
            self._stats.rejected_count += 1
        self._stats.anomaly_warning_count += sum(1 for finding in findings if finding.category == ValidationCategory.ANOMALY)

    def _update_monotonic_state(self, context: ValidationContext) -> None:
        if not self._config.enforce_monotonic_timestamps:
            return
        if context.sequence_key is None or context.source_timestamp is None:
            return
        self._last_timestamp_by_sequence[context.sequence_key] = context.source_timestamp

    def _update_anomaly_state(self, context: ValidationContext) -> None:
        for field_name in self._config.numeric_anomaly_fields:
            value = context.payload.get(field_name)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                self._numeric_history[field_name].append(float(value))

    async def _validate_schema_and_types(self, context: ValidationContext) -> ValidationRuleResult:
        findings: list[ValidationFinding] = []
        payload = dict(context.payload)
        for field_spec in context.schema.fields:
            raw_value = payload.get(field_spec.name)
            if field_spec.name not in payload:
                if field_spec.required:
                    findings.append(
                        ValidationFinding(
                            rule_name="schema_and_type_validation",
                            severity=ValidationSeverity.ERROR,
                            category=ValidationCategory.MISSING_FIELD,
                            message="required field missing",
                            field_name=field_spec.name,
                        )
                    )
                continue
            if raw_value is None:
                if not field_spec.allow_none:
                    findings.append(
                        ValidationFinding(
                            rule_name="schema_and_type_validation",
                            severity=ValidationSeverity.ERROR,
                            category=ValidationCategory.TYPE,
                            message="field value cannot be null",
                            field_name=field_spec.name,
                        )
                    )
                continue
            coerced_value = raw_value
            if field_spec.coerce_numeric:
                coerced_value = _coerce_numeric(raw_value)
                if coerced_value is None:
                    findings.append(
                        ValidationFinding(
                            rule_name="schema_and_type_validation",
                            severity=ValidationSeverity.ERROR,
                            category=ValidationCategory.TYPE,
                            message="numeric coercion failed",
                            field_name=field_spec.name,
                            value=_safe_scalar(raw_value),
                        )
                    )
                    continue
                payload[field_spec.name] = coerced_value
            if field_spec.coerce_datetime:
                coerced_datetime = _coerce_datetime(raw_value)
                if coerced_datetime is None:
                    findings.append(
                        ValidationFinding(
                            rule_name="schema_and_type_validation",
                            severity=ValidationSeverity.ERROR,
                            category=ValidationCategory.TYPE,
                            message="datetime coercion failed",
                            field_name=field_spec.name,
                            value=_safe_scalar(raw_value),
                        )
                    )
                    continue
                payload[field_spec.name] = coerced_datetime.isoformat()
                coerced_value = payload[field_spec.name]
            if field_spec.coerce_date:
                coerced_date = _coerce_date(raw_value)
                if coerced_date is None:
                    findings.append(
                        ValidationFinding(
                            rule_name="schema_and_type_validation",
                            severity=ValidationSeverity.ERROR,
                            category=ValidationCategory.TYPE,
                            message="date coercion failed",
                            field_name=field_spec.name,
                            value=_safe_scalar(raw_value),
                        )
                    )
                    continue
                payload[field_spec.name] = coerced_date.isoformat()
                coerced_value = payload[field_spec.name]
            if field_spec.accepted_types and not isinstance(raw_value, field_spec.accepted_types):
                if not (
                    field_spec.coerce_numeric or field_spec.coerce_datetime or field_spec.coerce_date
                ):
                    findings.append(
                        ValidationFinding(
                            rule_name="schema_and_type_validation",
                            severity=ValidationSeverity.ERROR,
                            category=ValidationCategory.TYPE,
                            message="field type not accepted",
                            field_name=field_spec.name,
                            value=_safe_scalar(raw_value),
                        )
                    )
                    continue
            numeric_value = _as_float(payload.get(field_spec.name))
            if field_spec.min_value is not None and numeric_value is not None and numeric_value < field_spec.min_value:
                findings.append(
                    ValidationFinding(
                        rule_name="schema_and_type_validation",
                        severity=ValidationSeverity.ERROR,
                        category=ValidationCategory.RANGE,
                        message=f"value below minimum {field_spec.min_value}",
                        field_name=field_spec.name,
                        value=_safe_scalar(payload.get(field_spec.name)),
                    )
                )
            if field_spec.max_value is not None and numeric_value is not None and numeric_value > field_spec.max_value:
                findings.append(
                    ValidationFinding(
                        rule_name="schema_and_type_validation",
                        severity=ValidationSeverity.ERROR,
                        category=ValidationCategory.RANGE,
                        message=f"value above maximum {field_spec.max_value}",
                        field_name=field_spec.name,
                        value=_safe_scalar(payload.get(field_spec.name)),
                    )
                )
            if field_spec.allowed_values and payload.get(field_spec.name) not in field_spec.allowed_values:
                findings.append(
                    ValidationFinding(
                        rule_name="schema_and_type_validation",
                        severity=ValidationSeverity.ERROR,
                        category=ValidationCategory.BUSINESS_RULE,
                        message="value not in allowed set",
                        field_name=field_spec.name,
                        value=_safe_scalar(payload.get(field_spec.name)),
                    )
                )
        return ValidationRuleResult(findings=tuple(findings), mutated_payload=payload)

    async def _validate_unknown_fields(self, context: ValidationContext) -> ValidationRuleResult:
        if context.schema.allow_unknown_fields:
            return ValidationRuleResult()
        allowed_fields = set(context.schema.field_map)
        findings = [
            ValidationFinding(
                rule_name="unknown_field_validation",
                severity=ValidationSeverity.ERROR,
                category=ValidationCategory.SCHEMA,
                message="unknown field present in payload",
                field_name=field_name,
                value=_safe_scalar(context.payload[field_name]),
            )
            for field_name in context.payload
            if field_name not in allowed_fields
        ]
        return ValidationRuleResult(findings=tuple(findings))

    async def _validate_timestamp_monotonicity(self, context: ValidationContext) -> ValidationRuleResult:
        if not self._config.enforce_monotonic_timestamps:
            return ValidationRuleResult()
        if context.source_timestamp is None or context.sequence_key is None:
            return ValidationRuleResult()
        previous_timestamp = self._last_timestamp_by_sequence.get(context.sequence_key)
        if previous_timestamp is None:
            return ValidationRuleResult()
        if context.source_timestamp < previous_timestamp:
            return ValidationRuleResult(
                findings=(
                    ValidationFinding(
                        rule_name="timestamp_monotonicity_validation",
                        severity=ValidationSeverity.ERROR,
                        category=ValidationCategory.MONOTONICITY,
                        message="source timestamp moved backwards for sequence key",
                        field_name=self._config.source_timestamp_field,
                        value=context.source_timestamp.isoformat(),
                    ),
                )
            )
        return ValidationRuleResult()

    async def _validate_numeric_anomalies(self, context: ValidationContext) -> ValidationRuleResult:
        findings: list[ValidationFinding] = []
        for field_name in self._config.numeric_anomaly_fields:
            history = self._numeric_history[field_name]
            if len(history) < 10:
                continue
            value = context.payload.get(field_name)
            numeric_value = _as_float(value)
            if numeric_value is None:
                continue
            mean_value = sum(history) / len(history)
            variance = sum((item - mean_value) ** 2 for item in history) / len(history)
            std_dev = math.sqrt(variance)
            if std_dev <= 0:
                continue
            z_score = abs((numeric_value - mean_value) / std_dev)
            if z_score >= self._config.anomaly_z_score_threshold:
                findings.append(
                    ValidationFinding(
                        rule_name="numeric_anomaly_validation",
                        severity=ValidationSeverity.WARNING,
                        category=ValidationCategory.ANOMALY,
                        message=f"numeric anomaly detected with z-score {z_score:.2f}",
                        field_name=field_name,
                        value=_safe_scalar(value),
                    )
                )
        return ValidationRuleResult(findings=tuple(findings))



def _json_safe_mapping(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {str(key): _json_safe_value(value) for key, value in payload.items()}



def _json_safe_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe_value(item) for item in value]
    return str(value)



def _extract_timestamp(payload: Mapping[str, JsonValue], field_name: str | None) -> datetime | None:
    if field_name is None:
        return None
    raw_value = payload.get(field_name)
    return _coerce_datetime(raw_value)



def _build_sequence_key(payload: Mapping[str, JsonValue], fields: Sequence[str]) -> str | None:
    if not fields:
        return None
    parts: list[str] = []
    for field_name in fields:
        if field_name not in payload:
            return None
        parts.append(f"{field_name}={payload[field_name]}")
    return "|".join(parts)



def _coerce_numeric(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            if any(marker in text for marker in (".", "e", "E")):
                numeric = float(text)
                return numeric if math.isfinite(numeric) else None
            return int(text)
        except ValueError:
            return None
    return None



def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None



def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None



def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None



def _safe_scalar(value: Any) -> JsonValue | None:
    converted = _json_safe_value(value)
    if isinstance(converted, (dict, list)):
        return str(converted)
    return converted



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "JsonPrimitive",
    "JsonValue",
    "ValidationCategory",
    "ValidationContext",
    "ValidationDecision",
    "ValidationFieldSpec",
    "ValidationFinding",
    "ValidationPayload",
    "ValidationPipeline",
    "ValidationPipelineConfig",
    "ValidationPipelineError",
    "ValidationResult",
    "ValidationRule",
    "ValidationRuleResult",
    "ValidationSchema",
    "ValidationSeverity",
    "ValidationStats",
]
