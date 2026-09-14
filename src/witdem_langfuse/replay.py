"""Duckle transformation of explicit Witdem SDK wire records for historical replay.

Input must be an application outbox/export, never Langfuse observation metadata.
This module changes identity bindings only; it does not evaluate business truth.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .backfill import identity


class WireRecord(BaseModel):
    """Existing OSS /sdk/v1/records v1.0 envelope, not a new business schema."""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["1.0"]
    kind: Literal["event", "decision", "evaluation", "outcome", "metric"]
    event_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    trace_id: str | None = None
    span_id: str | None = None
    name: str = Field(min_length=1)
    value: Any = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ReplayPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["1.0"]
    source: Literal["application_records"]
    project_id: str = Field(min_length=1)
    source_execution_id: str = Field(min_length=1)
    source_trace_id: str = Field(min_length=1)
    records: list[WireRecord] = Field(min_length=1, max_length=100)


def remap(document):
    page = ReplayPage.model_validate(document)
    target = identity(page.project_id, "trace", page.source_trace_id, 16).hex()
    result, seen = [], set()
    for record in page.records:
        if record.execution_id != page.source_execution_id:
            raise ValueError("record execution does not match explicit binding")
        if record.trace_id is not None and record.trace_id != page.source_trace_id:
            raise ValueError("record trace does not match explicit binding")
        if record.event_id in seen:
            raise ValueError("duplicate event identity in replay page")
        seen.add(record.event_id)
        payload = record.model_dump(mode="json")
        payload["event_id"] = identity(
            page.project_id, "record:" + page.source_execution_id, record.event_id, 16
        ).hex()
        payload["execution_id"] = target
        payload["trace_id"] = target
        if record.span_id:
            payload["span_id"] = identity(
                page.project_id, page.source_trace_id, record.span_id, 8
            ).hex()
        result.append(payload)
    # Also reject NaN/Infinity inside arbitrary business values or attributes.
    json.dumps(result, allow_nan=False)
    return result


def process(row):
    """Duckle code.python contract: one bounded page in, one mapped page out."""
    return {
        "records_json": json.dumps(
            remap(json.loads(row["page_json"])), allow_nan=False, sort_keys=True
        )
    }
