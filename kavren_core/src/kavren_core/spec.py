"""The acceptance specification ("spec").

A spec is a closed, versioned description of what counts as done for one job.
It is stored in full on-chain as canonical JSON (see canonical.py), and
spec_hash = sha256(canonical bytes). Unknown fields are rejected.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import EVALUATOR_VERSION, SPEC_FORMAT
from .canonical import canonical_bytes, load_canonical, sha256_hex
from .formats import ALLOWED_INPUT_FORMATS, OUTPUT_FORMATS

REASON_CODES = ("INVALID_EMAIL", "MISSING_REQUIRED", "INVALID_DATE")
RECORD_ID_COLUMN = "record_id"
HELPER_COLUMNS = ("_outcome", "_merged_into", "_reason")
OUTCOMES = ("kept", "merged", "rejected")

_COLUMN_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Column(_Frozen):
    name: str
    type: Literal["text", "email", "date"]
    required: bool

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _COLUMN_NAME.fullmatch(v) or v == RECORD_ID_COLUMN:
            raise ValueError(f"invalid column name {v!r}")
        return v


class Dialect(_Frozen):
    encoding: Literal["utf-8"] = "utf-8"
    bom: Literal[False] = False
    delimiter: Literal[","] = ","
    quoting: Literal["rfc4180"] = "rfc4180"
    line_terminator: Literal["\n"] = "\n"
    header: Literal[True] = True
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=5 * 1024 * 1024)
    max_rows: int = Field(default=20_000, ge=1, le=20_000)


class DateRules(_Frozen):
    input_formats: tuple[str, ...]
    output_format: str

    @field_validator("input_formats")
    @classmethod
    def _inputs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v or len(set(v)) != len(v):
            raise ValueError("input_formats must be non-empty and unique")
        bad = [f for f in v if f not in ALLOWED_INPUT_FORMATS]
        if bad:
            raise ValueError(f"unsupported input formats {bad}")
        return v

    @field_validator("output_format")
    @classmethod
    def _output(cls, v: str) -> str:
        if v not in OUTPUT_FORMATS:
            raise ValueError(f"unsupported output format {v!r}")
        return v


class Spec(_Frozen):
    spec_format: Literal["kavren-spec/1"] = SPEC_FORMAT
    template_id: str
    template_version: int = Field(ge=1)
    evaluator_version: str = EVALUATOR_VERSION
    dialect: Dialect = Dialect()
    record_id_column: Literal["record_id"] = RECORD_ID_COLUMN
    columns: tuple[Column, ...]
    dedupe_key: tuple[str, ...]
    date_rules: DateRules
    text_rule: Literal["kavren-text/1"] = "kavren-text/1"
    email_rule: Literal["kavren-email/1"] = "kavren-email/1"
    max_reject_pct: int = Field(ge=0, le=100)
    reason_codes: tuple[str, ...] = REASON_CODES

    @field_validator("template_id")
    @classmethod
    def _tid(cls, v: str) -> str:
        if not re.fullmatch(r"^[a-z][a-z0-9-]{1,30}-v[0-9]+$", v):
            raise ValueError(f"invalid template_id {v!r}")
        return v

    @field_validator("reason_codes")
    @classmethod
    def _reasons(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(v) != REASON_CODES:
            raise ValueError("reason_codes must equal the protocol list")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "Spec":
        names = [c.name for c in self.columns]
        if not names or len(set(names)) != len(names):
            raise ValueError("columns must be non-empty with unique names")
        if not any(c.required for c in self.columns):
            raise ValueError("at least one column must be required")
        if not self.dedupe_key or len(set(self.dedupe_key)) != len(self.dedupe_key):
            raise ValueError("dedupe_key must be non-empty and unique")
        by_name = {c.name: c for c in self.columns}
        for k in self.dedupe_key:
            if k not in by_name:
                raise ValueError(f"dedupe_key column {k!r} not in columns")
            if not by_name[k].required:
                raise ValueError(f"dedupe_key column {k!r} must be required")
        return self

    # ------------------------------------------------------------ helpers
    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def work_header(self) -> tuple[str, ...]:
        return (RECORD_ID_COLUMN, *self.column_names)

    def submission_header(self) -> tuple[str, ...]:
        return (RECORD_ID_COLUMN, *HELPER_COLUMNS, *self.column_names)

    def to_json_obj(self) -> dict:
        return self.model_dump(mode="json")

    def canonical(self) -> bytes:
        return canonical_bytes(self.to_json_obj())

    def hash(self) -> str:
        return sha256_hex(self.canonical())


def load_spec(data: bytes) -> Spec:
    """Load a spec from bytes, requiring canonical form and a valid schema."""
    obj = load_canonical(data)
    spec = Spec.model_validate(_tuplify(obj), strict=False)
    if spec.canonical() != data:
        raise ValueError("spec bytes do not round-trip to the same canonical form")
    return spec


def _tuplify(v):
    if isinstance(v, list):
        return tuple(_tuplify(x) for x in v)
    if isinstance(v, dict):
        return {k: _tuplify(x) for k, x in v.items()}
    return v
