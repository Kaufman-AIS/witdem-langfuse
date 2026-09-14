"""Resumable, bounded Langfuse backfill into the existing Witdem OSS receiver.

One checkpoint database belongs to one source, destination and frozen range.
Delivery is at least once: an ambiguous acknowledgement replays identical bytes
and stable span identities. No business facts are synthesized from observations.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from importlib.resources import files
from pathlib import Path
from urllib.error import URLError

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from .client import Client, SourceError
from .http_policy import request
from .quota import SharedBudget

NORMALIZATION_VERSION = 2


def retry_delay(value, now, failures):
    """Honor numeric and HTTP-date Retry-After; use bounded backoff otherwise."""
    if value is not None:
        try:
            delay = float(value)
            if math.isfinite(delay) and delay >= 0:
                return max(1, delay)
        except (ValueError, TypeError):
            try:
                parsed = parsedate_to_datetime(str(value))
                if parsed.tzinfo is not None:
                    return max(1, parsed.timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
    return min(300, 2 ** min(failures, 8))


def identity(project, kind, value, size):
    return hashlib.sha256(
        json.dumps([project, kind, value], separators=(",", ":")).encode()
    ).digest()[:size]


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("source timestamp must include a timezone")
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    result = (
        delta.days * 86400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1000
    if result <= 0:
        raise ValueError("invalid source timestamp")
    return result


def token_usage(row):
    """Langfuse usage aggregates can mix tokens, bytes, pages and vectors.

    Reassemble only documented exclusive token buckets. Unknown usage types
    remain source usage details and never silently become tokens.
    """
    if row.get("type") not in ("GENERATION", "EMBEDDING"):
        return {}
    details = row.get("usageDetails")
    if not isinstance(details, dict):
        return {}
    result = {}
    for direction in ("input", "output"):
        keys = [
            key
            for key in details
            if key == direction
            or (key.startswith(direction + "_") and key.endswith("_tokens"))
            or (
                direction == "input"
                and key in ("cache_read_input_tokens", "cache_creation_input_tokens")
            )
        ]
        values = [details[key] for key in keys]
        if values and all(type(value) is int and value >= 0 for value in values):
            result[direction + "_tokens"] = sum(values)
    if "input_tokens" in result and "output_tokens" in result:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def encode(rows, project):
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    resource.resource.attributes.add(
        key="service.name"
    ).value.string_value = "langfuse-import"
    scope = resource.scope_spans.add()
    scope.scope.name = "witdem.langfuse.backfill"
    scope.scope.version = str(NORMALIZATION_VERSION)
    for row in rows:
        if row.get("projectId") != project:
            raise ValueError("source project mismatch")
        for key in ("id", "traceId", "startTime"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError("missing source identity or timestamp")
        span = scope.spans.add()
        span.trace_id = identity(project, "trace", row["traceId"], 16)
        span.span_id = identity(project, row["traceId"], row["id"], 8)
        if row.get("parentObservationId"):
            span.parent_span_id = identity(
                project, row["traceId"], row["parentObservationId"], 8
            )
        span.name = str(row.get("name") or row.get("type") or "Langfuse observation")[
            :512
        ]
        span.kind = 1
        span.start_time_unix_nano = timestamp(row["startTime"])
        if row.get("endTime"):
            span.end_time_unix_nano = timestamp(row["endTime"])
            if span.end_time_unix_nano < span.start_time_unix_nano:
                raise ValueError("observation ends before it starts")
        if row.get("level") == "ERROR":
            span.status.code = 2
        attributes = {
            "langfuse.project.id": project,
            "langfuse.trace.id": row["traceId"],
            "langfuse.observation.id": row["id"],
            "langfuse.observation.type": row.get("type", "SPAN"),
            "witdem.langfuse.import.version": str(NORMALIZATION_VERSION),
        }
        # Explicit opt-in fetch; retain only descriptive workflow identifiers.
        metadata = row.get("metadata") or {}
        for key in (
            "haystack.component.name",
            "haystack.component.type",
            "witdem.workflow.id",
        ):
            value = metadata.get("attributes." + key)
            if isinstance(value, str) and len(value) <= 512:
                attributes[key] = value
        if row.get("model"):
            attributes["gen_ai.response.model"] = row["model"]
        if row.get("type") in ("GENERATION", "EMBEDDING"):
            attributes["gen_ai.operation.name"] = (
                "embeddings" if row["type"] == "EMBEDDING" else "chat"
            )
        for target, value in token_usage(row).items():
            attributes["gen_ai.usage." + target] = value
        for key, value in attributes.items():
            item = span.attributes.add(key=key)
            if type(value) is int:
                item.value.int_value = value
            else:
                item.value.string_value = str(value)
    return request.SerializeToString(deterministic=True)


def process(row):
    """Duckle code.python node; only the allowlisted OTLP projection leaves ETL."""
    document = json.loads(row["page_json"])
    return {
        "payload": base64.b64encode(
            encode(document["rows"], document["project"])
        ).decode()
    }


def duckle_encode(rows, project):
    executable = os.environ.get("DUCKLE_EXECUTABLE") or shutil.which("duckle")
    if not executable:
        raise RuntimeError("Duckle executable is required for backfills")
    document = json.dumps(
        {"page_json": json.dumps({"rows": rows, "project": project}, allow_nan=False)},
        allow_nan=False,
    )
    if len(document.encode()) > 32 * 1024 * 1024:
        raise ValueError("backfill page exceeds 32 MiB; reduce page size")
    with tempfile.TemporaryDirectory(prefix="witdem-langfuse-backfill-") as directory:
        root = Path(directory)
        (root / "input.jsonl").write_text(document + "\n")
        pipeline = root / "backfill.pipeline.json"
        pipeline.write_text(
            files("witdem_langfuse")
            .joinpath("pipelines/backfill.pipeline.json")
            .read_text()
        )
        result = subprocess.run(
            [
                executable,
                "--pipeline",
                str(pipeline),
                "--workspace",
                str(root),
                "--log-dir",
                str(root / "logs"),
                "--name",
                "langfuse-backfill",
                "--manifest",
            ],
            env={
                **os.environ,
                "DUCKLE_PYTHON_BIN": sys.executable,
                "DUCKLE_THREADS": "2",
                "DUCKLE_MEMORY_LIMIT": "256MB",
            },
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Duckle backfill failed; checkpoint has not advanced")
        output = root / "output.jsonl"
        if not output.is_file() or output.stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError("invalid Duckle backfill output")
        records = output.read_text().splitlines()
        if len(records) != 1:
            raise RuntimeError("Duckle must emit exactly one normalized page")
        payload = base64.b64decode(json.loads(records[0])["payload"], validate=True)
        decoded = ExportTraceServiceRequest.FromString(payload)
        if sum(
            len(s.spans) for r in decoded.resource_spans for s in r.scope_spans
        ) != len(rows):
            raise RuntimeError("Duckle output observation count mismatch")
        return payload


class Backfill:
    def __init__(
        self,
        checkpoint,
        client,
        receiver,
        start,
        end,
        *,
        send,
        page_size=100,
        transform=duckle_encode,
        clock=time.time,
        budget=None,
        workflow_context=False,
    ):
        a, b = (datetime.fromisoformat(v) for v in (start, end))
        if a.tzinfo is None or b.tzinfo is None or a >= b or not 1 <= page_size <= 1000:
            raise ValueError("invalid backfill range or page size")
        self.workflow_context = workflow_context
        self.client, self.send = client, send
        self.transform = transform
        self.clock = clock
        self.budget = budget
        self.path = Path(checkpoint)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = {
            "version": NORMALIZATION_VERSION,
            "source": client.base_url,
            "project": client.project_id,
            "receiver": receiver,
            "start": a.astimezone(UTC).isoformat(),
            "end": b.astimezone(UTC).isoformat(),
            "page_size": page_size,
        }

        if workflow_context:
            self.config["workflow_context"] = 1

    def run(self, max_pages=10):
        if max_pages < 1:
            raise ValueError("max pages must be positive")
        # Locks are released by the OS on crashes. A second writer fails visibly.
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(self.path) as db:
                os.chmod(self.path, 0o600)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL)"
                )
                saved = db.execute(
                    "SELECT document FROM checkpoint WHERE id=1"
                ).fetchone()
                state = (
                    json.loads(saved[0])
                    if saved
                    else {
                        "config": self.config,
                        "current": self.config["start"],
                        "cursor": None,
                        "seen": [],
                        "pending": None,
                        "pages": 0,
                        "rows": 0,
                        "complete": False,
                    }
                )
                if state["config"] != self.config:
                    raise ValueError(
                        "checkpoint belongs to a different source, destination, range or version"
                    )
                state.setdefault("retry_at", 0)
                state.setdefault("failures", 0)
                state.setdefault("cursor_restarts", 0)
                state.setdefault("last_error", None)

                def save():
                    db.execute(
                        "INSERT OR REPLACE INTO checkpoint VALUES(1,?)",
                        (json.dumps(state),),
                    )
                    db.commit()

                save()

                def defer(status, retry_after=None):
                    state["failures"] += 1
                    state["last_error"] = status
                    state["retry_at"] = self.clock() + retry_delay(
                        retry_after, self.clock(), state["failures"]
                    )
                    save()

                for _ in range(max_pages):
                    if state["complete"] or state["retry_at"] > self.clock():
                        break
                    window_end = min(
                        datetime.fromisoformat(state["current"]) + timedelta(days=1),
                        datetime.fromisoformat(self.config["end"]),
                    ).isoformat()
                    if state["pending"] is None:
                        if self.budget is not None:
                            deadline = self.budget.acquire()
                            if deadline:
                                state["retry_at"] = deadline
                                state["last_error"] = "shared_source_budget"
                                save()
                                break
                        try:
                            rows, cursor = self.client.page(
                                state["current"],
                                window_end,
                                cursor=state["cursor"],
                                limit=self.config["page_size"],
                                fields="core,basic,time,usage,model"
                                + (",metadata" if self.workflow_context else ""),
                            )
                        except SourceError as exc:
                            if (
                                exc.status == 400
                                and state["cursor"]
                                and state["cursor_restarts"] == 0
                            ):
                                state["cursor"] = None
                                state["seen"] = []
                                state["cursor_restarts"] = 1
                                state["last_error"] = "source_cursor_restarted"
                                save()
                                continue
                            if exc.status == 429 or exc.status >= 500:
                                defer("source_http_" + str(exc.status), exc.retry_after)
                                if self.budget is not None:
                                    self.budget.defer_until(state["retry_at"])
                                break
                            raise
                        except (OSError, URLError):
                            defer("source_transport_error")
                            break
                        if cursor and cursor in state["seen"]:
                            raise ValueError(
                                "source cursor cycle; checkpoint preserved"
                            )
                        state["pending"] = {
                            "payload": base64.b64encode(
                                self.transform(rows, self.client.project_id)
                            ).decode(),
                            "cursor": cursor,
                            "rows": len(rows),
                        }
                        save()  # Persist exact bytes BEFORE any delivery attempt.
                    pending = state["pending"]
                    if pending["rows"]:
                        try:
                            self.send(base64.b64decode(pending["payload"]))
                        except httpx.HTTPStatusError as exc:
                            status = exc.response.status_code
                            if status == 429 or status >= 500:
                                defer(
                                    "receiver_http_" + str(status),
                                    exc.response.headers.get("Retry-After"),
                                )
                                break
                            raise
                        except httpx.TransportError:
                            defer("receiver_transport_error")
                            break
                    state["pages"] += 1
                    state["rows"] += pending["rows"]
                    state["cursor"] = pending["cursor"]
                    if pending["cursor"]:
                        state["seen"].append(pending["cursor"])
                    else:
                        state["current"] = window_end
                        state["seen"] = []
                        state["cursor_restarts"] = 0
                        state["complete"] = window_end == self.config["end"]
                    state["pending"] = None
                    state["failures"] = 0
                    state["retry_at"] = 0
                    state["last_error"] = None
                    save()  # Advance only after the receiver acknowledged delivery.
                return {
                    key: state[key]
                    for key in (
                        "current",
                        "pages",
                        "rows",
                        "complete",
                        "retry_at",
                        "last_error",
                    )
                }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--receiver", required=True)
    parser.add_argument("--from-time", required=True)
    parser.add_argument("--to-time", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument(
        "--quota-db",
        type=Path,
        default=Path.home() / ".local/state/witdem-langfuse/quota.sqlite",
    )
    parser.add_argument(
        "--quota-group",
        help="shared allowance group; defaults to the Langfuse source URL",
    )
    parser.add_argument("--requests-per-minute", type=float, default=30)
    parser.add_argument(
        "--follow",
        action="store_true",
        help="continue until complete, honoring durable cooldowns; Ctrl-C leaves a resumable checkpoint",
    )
    args = parser.parse_args()
    # Apply the same operator-controlled URL policy to both endpoints.
    Client(args.receiver, "", "", args.project, allow_http=args.allow_http)
    source = Client(
        args.source,
        os.environ["LANGFUSE_PUBLIC_KEY"],
        os.environ["LANGFUSE_SECRET_KEY"],
        args.project,
        allow_http=args.allow_http,
    )
    headers = {"Content-Type": "application/x-protobuf"}
    if os.environ.get("WITDEM_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["WITDEM_API_KEY"]
    with httpx.Client(timeout=30, follow_redirects=False) as http:

        def send(payload):
            response = request(
                http,
                "POST",
                args.receiver.rstrip("/") + "/v1/traces",
                content=payload,
                headers=headers,
            )
            response.raise_for_status()
            ack = ExportTraceServiceResponse.FromString(response.content)
            if ack.partial_success.rejected_spans or ack.partial_success.error_message:
                raise RuntimeError(
                    "OSS receiver reported partial success; page remains pending"
                )

        backfill = Backfill(
            args.checkpoint,
            source,
            args.receiver,
            args.from_time,
            args.to_time,
            send=send,
            page_size=args.page_size,
            budget=None
            if os.getenv("WITDEM_JOB_STATE")
            else SharedBudget(
                args.quota_db,
                args.quota_group or source.base_url,
                requests_per_minute=args.requests_per_minute,
            ),
        )
        while True:
            result = backfill.run(1 if args.follow else args.max_pages)
            print(json.dumps(result), flush=True)
            if result["complete"] or not args.follow:
                break
            # One page per iteration with conservative per-process pacing.
            # Longer cooldowns are checked without issuing network requests.
            time.sleep(min(30, max(2, result["retry_at"] - time.time())))


if __name__ == "__main__":
    main()
