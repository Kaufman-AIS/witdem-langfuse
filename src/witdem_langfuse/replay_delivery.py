"""Immutable-manifest, resumable delivery of Duckle-mapped application records."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from importlib.resources import files
from pathlib import Path

import httpx

from .backfill import retry_delay
from .client import Client
from .http_policy import request
from .replay import ReplayPage, WireRecord


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def duckle_page(page, *, pipeline_name="replay.pipeline.json", validate_page=True):
    if validate_page:
        ReplayPage.model_validate(page)
    encoded = canonical({"page_json": canonical(page)}) + "\n"
    if len(encoded.encode()) > 32 * 1024 * 1024:
        raise ValueError("Duckle replay page exceeds 32 MiB")
    executable = os.environ.get("DUCKLE_EXECUTABLE") or shutil.which("duckle")
    if not executable:
        raise RuntimeError("Duckle is required for application record replay")
    with tempfile.TemporaryDirectory(prefix="witdem-replay-") as directory:
        root = Path(directory)
        (root / "input.jsonl").write_text(encoded)
        pipeline = root / "replay.pipeline.json"
        pipeline.write_text(
            files("witdem_langfuse").joinpath("pipelines/" + pipeline_name).read_text()
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
                "record-replay",
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
        output = root / "output.jsonl"
        if (
            result.returncode
            or not output.is_file()
            or output.stat().st_size > 32 * 1024 * 1024
        ):
            raise RuntimeError("Duckle replay failed; no records sent")
        lines = output.read_text().splitlines()
        if len(lines) != 1:
            raise ValueError("Duckle must emit one mapped page")
        records = json.loads(json.loads(lines[0])["records_json"])
        if validate_page and len(records) != len(page["records"]):
            raise ValueError("Duckle record count mismatch")
        return [
            WireRecord.model_validate(row).model_dump(mode="json") for row in records
        ]


class ReplayDelivery:
    def __init__(
        self,
        manifest,
        checkpoint,
        receiver,
        *,
        send,
        transform=duckle_page,
        clock=time.time,
    ):
        self.manifest, self.checkpoint = Path(manifest), Path(checkpoint)
        self.receiver, self.send, self.transform, self.clock = (
            receiver,
            send,
            transform,
            clock,
        )

    def run(self, max_records=100):
        if max_records < 1:
            raise ValueError("record budget must be positive")
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        with self.checkpoint.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(self.checkpoint) as db:
                os.chmod(self.checkpoint, 0o600)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS manifest(config TEXT NOT NULL, retry_at REAL NOT NULL, failures INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS records(event_id TEXT PRIMARY KEY, body TEXT NOT NULL, receipt TEXT)"
                )
                with self.manifest.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                config = canonical(
                    {"version": 1, "receiver": self.receiver, "sha256": digest}
                )
                saved = db.execute(
                    "SELECT config,retry_at,failures FROM manifest"
                ).fetchone()
                if saved and saved[0] != config:
                    raise ValueError(
                        "manifest content or receiver changed; checkpoint is immutable"
                    )
                if not saved:
                    # Validate and transform every page before the first network write.
                    # A conflict rolls back the entire preflight transaction.
                    seen_digest = hashlib.sha256()
                    with self.manifest.open("rb") as source:
                        while line := source.readline(32 * 1024 * 1024 + 1):
                            if len(line) > 32 * 1024 * 1024:
                                raise ValueError("manifest page exceeds 32 MiB")
                            seen_digest.update(line)
                            if not line.strip():
                                continue
                            page = json.loads(line)
                            for record in self.transform(page):
                                body = canonical(record)
                                existing = db.execute(
                                    "SELECT body FROM records WHERE event_id=?",
                                    (record["event_id"],),
                                ).fetchone()
                                if existing and existing[0] != body:
                                    raise ValueError(
                                        "conflicting event identity across replay pages"
                                    )
                                db.execute(
                                    "INSERT OR IGNORE INTO records(event_id,body) VALUES(?,?)",
                                    (record["event_id"], body),
                                )
                    if seen_digest.hexdigest() != digest:
                        raise ValueError("manifest changed during preflight")
                    if db.execute("SELECT count(*) FROM records").fetchone()[0] == 0:
                        raise ValueError("manifest contains no records")
                    db.execute("INSERT INTO manifest VALUES(?,0,0)", (config,))
                    db.commit()
                    saved = (config, 0, 0)
                retry_at, failures = saved[1:]
                if retry_at <= self.clock():
                    pending = db.execute(
                        "SELECT event_id,body FROM records WHERE receipt IS NULL ORDER BY rowid LIMIT ?",
                        (max_records,),
                    ).fetchall()
                    for event_id, body in pending:
                        try:
                            receipt = self.send(body.encode())
                            if (
                                not isinstance(receipt, dict)
                                or receipt.get("event_id") != event_id
                                or receipt.get("status") != "accepted"
                            ):
                                raise ValueError(
                                    "receiver acknowledgement does not match pending record"
                                )
                        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                            header = None
                            if isinstance(exc, httpx.HTTPStatusError):
                                if (
                                    exc.response.status_code != 429
                                    and exc.response.status_code < 500
                                ):
                                    raise
                                header = exc.response.headers.get("Retry-After")
                            failures += 1
                            retry_at = self.clock() + retry_delay(
                                header, self.clock(), failures
                            )
                            db.execute(
                                "UPDATE manifest SET retry_at=?,failures=?",
                                (retry_at, failures),
                            )
                            db.commit()
                            break
                        db.execute(
                            "UPDATE records SET receipt=? WHERE event_id=?",
                            (canonical(receipt), event_id),
                        )
                        db.execute("UPDATE manifest SET retry_at=0,failures=0")
                        db.commit()
                        retry_at, failures = 0, 0
                total, acknowledged = db.execute(
                    "SELECT count(*),count(receipt) FROM records"
                ).fetchone()
                return {
                    "records": total,
                    "acknowledged": acknowledged,
                    "complete": total == acknowledged,
                    "retry_at": retry_at,
                }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--receiver", required=True)
    parser.add_argument("--max-records", type=int, default=100)
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    Client(args.receiver, "", "", "", allow_http=args.allow_http)
    headers = {"Content-Type": "application/json"}
    if os.environ.get("WITDEM_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["WITDEM_API_KEY"]
    with httpx.Client(timeout=30, follow_redirects=False) as http:

        def send(body):
            result = request(
                http,
                "POST",
                args.receiver.rstrip("/") + "/sdk/v1/records",
                content=body,
                headers=headers,
            )
            result.raise_for_status()
            return result.json()

        replay = ReplayDelivery(
            args.manifest, args.checkpoint, args.receiver, send=send
        )
        while True:
            result = replay.run(args.max_records)
            print(json.dumps(result), flush=True)
            if result["complete"] or not args.follow:
                break
            time.sleep(min(30, max(1, result["retry_at"] - time.time())))


if __name__ == "__main__":
    main()
