"""Synthetic Langfuse protocol fixture -> real container/Duckle -> identity sink.

Optionally forwards ingestion to an actual Witdem OSS receiver. This is NOT a
Langfuse server throughput benchmark. No private traces or credentials used.
"""

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import yaml
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

ROOT = Path(__file__).resolve().parents[1]


def trace_id(n):
    return hashlib.md5(f"synthetic-{n}".encode()).hexdigest()


class Fixture:
    def __init__(self, root, receiver=None, faults=False):
        self.root, self.receiver, self.faults = root, receiver, faults
        self.requests, self.fired, self.bytes_in = [], set(), 0
        self.lock = threading.Lock()
        with sqlite3.connect(root / "sink.sqlite") as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, digest TEXT, PRIMARY KEY(kind,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries(kind TEXT, count INTEGER)"
            )

    def save(self, kind, key, data):
        self.save_many([(kind, key, data)])

    def save_many(self, records):
        # One transaction per received page, so the fixture does not introduce
        # an artificial per-span fsync bottleneck absent from batch ingestion.
        with sqlite3.connect(self.root / "sink.sqlite", timeout=30) as db:
            for kind, key, data in records:
                digest = hashlib.sha256(data).hexdigest()
                old = db.execute(
                    "SELECT digest FROM records WHERE kind=? AND id=?", (kind, key)
                ).fetchone()
                if old and old[0] != digest:
                    raise ValueError("same identity received different body")
                db.execute(
                    "INSERT OR IGNORE INTO records VALUES(?,?,?)", (kind, key, digest)
                )

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, body, status=200, headers=None):
                encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                parsed = urlsplit(self.path)
                with fixture.lock:
                    fixture.requests.append(
                        {"method": "GET", "path": parsed.path, "at": time.time()}
                    )
                    key = (
                        "get429-scores"
                        if parsed.path.endswith("/scores")
                        else "get429-observations"
                    )
                    if fixture.faults and key not in fixture.fired:
                        fixture.fired.add(key)
                        self.respond({}, 429, {"Retry-After": "1"})
                        return
                parts = parsed.path.split("/")
                n = int(parts[2])
                trace = trace_id(n)
                query = parse_qs(parsed.query)
                offset = int(query.get("cursor", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
                if parsed.path.endswith("/scores"):
                    data = []
                    for i in range(offset, min(n, offset + limit)):
                        name = (
                            ["evidence_completeness", "extraction_confidence"][i]
                            if i < 2
                            else "unrelated_diagnostic"
                        )
                        data.append(
                            {
                                "id": f"score-{n}-{i}",
                                "projectId": "benchmark",
                                "name": name,
                                "value": 0.9,
                                "dataType": "NUMERIC",
                                "source": "EVAL",
                                "comment": "SYNTHETIC benchmark evaluation",
                                "subject": {"kind": "trace", "id": trace},
                            }
                        )
                else:
                    if "traceId" in query:
                        limit = min(limit, n)
                    data = [
                        {
                            "id": f"{i + 1:016x}",
                            "traceId": trace,
                            "projectId": "benchmark",
                            "parentObservationId": None
                            if i == 0
                            else "0000000000000001",
                            "name": "SYNTHETIC benchmark step",
                            "type": "SPAN",
                            "startTime": "2026-09-01T00:00:00Z",
                            "endTime": "2026-09-01T00:00:01Z",
                            "level": "DEFAULT",
                        }
                        for i in range(offset, min(n, offset + limit))
                    ]
                self.respond(
                    {
                        "data": data,
                        "meta": {
                            "cursor": str(offset + limit)
                            if offset + limit < n
                            else None
                        },
                    }
                )

            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                with fixture.lock:
                    fixture.bytes_in += len(data)
                    fixture.requests.append(
                        {"method": "POST", "path": self.path, "at": time.time()}
                    )
                    key = "post503-" + self.path.rsplit("/", 1)[-1]
                    if fixture.faults and key not in fixture.fired:
                        fixture.fired.add(key)
                        self.respond({}, 503, {"Retry-After": "1"})
                        return
                if self.path.endswith("/v1/traces"):
                    req = ExportTraceServiceRequest.FromString(data)
                    spans = [
                        s
                        for r in req.resource_spans
                        for scope in r.scope_spans
                        for s in scope.spans
                    ]
                    fixture.save_many(
                        (
                            "span",
                            span.trace_id.hex() + ":" + span.span_id.hex(),
                            span.SerializeToString(deterministic=True),
                        )
                        for span in spans
                    )
                    ack = ExportTraceServiceResponse().SerializeToString()
                    suffix = "/v1/traces"
                elif self.path.endswith("/sdk/v1/records"):
                    record = json.loads(data)
                    fixture.save("semantic", record["event_id"], data)
                    ack = json.dumps(
                        {"status": "accepted", "event_id": record["event_id"]}
                    ).encode()
                    suffix = "/sdk/v1/records"
                else:
                    score = json.loads(data)
                    fixture.save("writeback", score["id"], data)
                    self.respond({"id": score["id"]})
                    return
                if fixture.receiver:
                    response = httpx.post(
                        fixture.receiver + suffix,
                        content=data,
                        headers={
                            "Content-Type": "application/x-protobuf"
                            if suffix == "/v1/traces"
                            else "application/json"
                        },
                        timeout=60,
                    )
                    response.raise_for_status()
                    ack = response.content
                with fixture.lock:
                    if fixture.faults and "ack_loss" not in fixture.fired:
                        fixture.fired.add("ack_loss")
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        self.connection.close()
                        return
                self.respond(ack)

        return Handler


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], check=check, text=True, capture_output=True
    ).stdout.strip()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--sizes", default="1000,10000,100000")
    p.add_argument("--image", default="witdem-langfuse-worker:stage12")
    p.add_argument("--receiver", help="Optional actual OSS receiver on the host")
    p.add_argument("--faults", action="store_true")
    p.add_argument("--interrupt", action="store_true")
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    fixture = Fixture(a.output, a.receiver, a.faults)
    server = ThreadingHTTPServer(("0.0.0.0", 0), fixture.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    source = f"http://host.docker.internal:{server.server_port}"
    results = []
    image_id = docker("image", "inspect", a.image, "--format", "{{.Id}}")
    for n in map(int, a.sizes.split(",")):
        case = a.output / str(n)
        case.mkdir()
        (case / "state").mkdir()
        (case / "config").mkdir()
        contract = yaml.safe_load(
            (ROOT / "examples/cuad-evaluations/contract.yaml").read_text()
        )
        (case / "config/contract.yaml").write_text(yaml.safe_dump(contract))
        bindings = {
            "version": 1,
            "requirements": {
                key: {
                    "evaluation": ev,
                    "name": ev,
                    "source": "EVAL",
                    "subject": "trace",
                    "score_id": f"score-{n}-{i}",
                }
                for i, (key, ev) in enumerate(
                    zip(contract["goal"]["requirements"], contract["evaluations"])
                )
            },
        }
        (case / "config/bindings.yaml").write_text(yaml.safe_dump(bindings))
        common = [
            "--source",
            source + f"/case/{n}",
            "--project",
            "benchmark",
            "--from-time",
            "2026-09-01T00:00:00Z",
            "--to-time",
            "2026-09-02T00:00:00Z",
            "--allow-http",
        ]
        tasks = [
            {
                "id": "observations",
                "command": "backfill",
                "arguments": common
                + [
                    "--receiver",
                    source,
                    "--checkpoint",
                    "/state/traces.sqlite",
                    "--max-pages",
                    "10",
                    "--page-size",
                    "1000",
                ],
            },
            {
                "id": "assessment",
                "command": "evaluations",
                "arguments": common
                + [
                    "--trace",
                    trace_id(n),
                    "--contract",
                    "/config/contract.yaml",
                    "--bindings",
                    "/config/bindings.yaml",
                    "--workspace",
                    "/state/assessment",
                    "--receiver",
                    source,
                    "--max-pages",
                    "1000",
                    "--application-result",
                    "approved_with_exceptions",
                ],
            },
        ]
        (case / "config/job.json").write_text(
            json.dumps({"version": 1, "tasks": tasks})
        )
        name = f"witdem-benchmark-{os.getpid()}-{n}"
        state_mount = str((case / "state").resolve())
        if a.interrupt:
            state_mount = name + "-state"
            docker("volume", "create", state_mount)
        base = [
            "run",
            "-d",
            "--name",
            name,
            "--init",
            "--cpus",
            "2",
            "--memory",
            "1g",
            "--pids-limit",
            "128",
            "--add-host",
            "host.docker.internal:host-gateway",
            "-e",
            "WITDEM_REQUESTS_PER_MINUTE=60000",
            "-e",
            "LANGFUSE_PUBLIC_KEY=synthetic",
            "-e",
            "LANGFUSE_SECRET_KEY=synthetic",
            "-v",
            state_mount + ":/state",
            "-v",
            str((case / "config").resolve()) + ":/config:ro",
            image_id,
        ]
        started = time.monotonic()
        request_start = len(fixture.requests)
        docker(*base)
        if a.interrupt:
            deadline = time.monotonic() + 90
            while len(fixture.requests) - request_start < 4:
                if time.monotonic() > deadline:
                    raise RuntimeError("worker made no progress")
                time.sleep(0.2)
            contender = base.copy()
            contender.remove("-d")
            contender[contender.index(name)] = name + "-contender"
            conflict = subprocess.run(
                ["docker", *contender], text=True, capture_output=True, check=False
            )
            assert conflict.returncode == 3 and "job_busy" in conflict.stdout, (
                "overlapping job was not rejected"
            )
            docker("rm", name + "-contender")
            docker("kill", "--signal=KILL", name)
            docker("wait", name)
            docker("start", name)
        code = docker("wait", name)
        logs = docker("logs", name)
        (case / "worker.ndjson").write_text(logs)
        inspect = json.loads(docker("inspect", name))[0]
        if code != "0":
            raise RuntimeError(
                f"container failed: {code}; inspect {case}/worker.ndjson"
            )
        metrics = [
            json.loads(line) for line in logs.splitlines() if line.startswith("{")
        ]
        completed = [m for m in metrics if m.get("event") == "job_completed"][-1]
        with sqlite3.connect(a.output / "sink.sqlite") as db:
            # Cases have distinct trace and event identities.
            counts = dict(db.execute("SELECT kind,count(*) FROM records GROUP BY kind"))
        expected_spans = sum(r["observations"] for r in results) + n
        assert counts.get("span") == expected_spans, counts
        assert counts.get("semantic") == 7 * (len(results) + 1), counts
        if a.interrupt:
            docker("cp", name + ":/state/.", str(case / "state"))
        derived = json.loads((case / "state/assessment/records.json").read_text())[-1]
        assert derived["attributes"]["product_goal_achieved"] is True
        saved = json.loads((case / "state/assessment/assessment.jsonl").read_text())
        assert len(saved["scores"]) == 2, "unrelated scores retained in assessment"
        detail = {
            "summary": {
                "trace_id": derived["trace_id"],
                "execution_id": derived["execution_id"],
                "product_goal_reported": True,
            },
            "outcomes": {
                "business": "approved_with_exceptions",
                "product_goal": {**derived["attributes"], "witdem.source": "sdk"},
            },
        }
        (case / "config/detail.json").write_text(json.dumps(detail))
        writeback = {
            "version": 1,
            "tasks": [
                {
                    "id": "writeback",
                    "command": "writeback",
                    "arguments": [
                        "--detail",
                        "/config/detail.json",
                        "--project",
                        "benchmark",
                        "--trace",
                        trace_id(n),
                        "--evidence-url",
                        "https://example.invalid/synthetic-evidence",
                        "--output",
                        "/state/writeback.json",
                        "--publish",
                        "--allow-http",
                    ],
                }
            ],
        }
        (case / "config/writeback.json").write_text(json.dumps(writeback))
        write_name = name + "-writeback"
        write_base = base.copy()
        write_base[write_base.index(name)] = write_name
        write_base[-1:-1] = ["-e", "LANGFUSE_BASE_URL=" + source + f"/case/{n}"]
        docker(
            *write_base,
            "--manifest",
            "/config/writeback.json",
            "--state",
            "/state/writeback-job",
        )
        write_code = docker("wait", write_name)
        (case / "writeback.ndjson").write_text(docker("logs", write_name))
        assert write_code == "0", "writeback job failed"
        with sqlite3.connect(a.output / "sink.sqlite") as db:
            counts = dict(db.execute("SELECT kind,count(*) FROM records GROUP BY kind"))
        assert counts.get("writeback") == 3 * (len(results) + 1), counts
        if a.interrupt:
            docker("cp", write_name + ":/state/.", str(case / "state"))
        docker("rm", write_name)
        elapsed = time.monotonic() - started
        before = len(fixture.requests)
        docker("start", name)
        assert docker("wait", name) == "0"
        assert len(fixture.requests) == before, "completed job issued new requests"
        row = {
            "observations": n,
            "source_scores": n,
            "wall_seconds": round(elapsed, 3),
            "observations_per_second": round(n / elapsed, 2),
            "http_requests": len(fixture.requests) - request_start,
            "peak_memory_mib": round(
                completed["container_peak_memory_bytes"] / 2**20, 2
            ),
            "cpu": completed["container_cpu"],
            "sink_cumulative_counts": counts,
            "state_bytes": sum(
                f.stat().st_size for f in (case / "state").rglob("*") if f.is_file()
            ),
            "oom_killed": inspect["State"]["OOMKilled"],
            "completed_rerun_no_requests": True,
            "interrupted_and_resumed": a.interrupt,
            "overlapping_job_rejected": a.interrupt,
        }
        results.append(row)
        print(json.dumps(row), flush=True)
        docker("rm", name)
        if a.interrupt:
            docker("volume", "rm", state_mount)
        (a.output / "results.json").write_text(
            json.dumps(
                {
                    "kind": "synthetic-source; actual container and Duckle; "
                    + (
                        "real OSS ingestion"
                        if a.receiver
                        else "SQLite identity-verifying sink"
                    ),
                    "limits": {
                        "cpus": 2,
                        "memory_gib": 1,
                        "source_requests_per_minute": 60000,
                    },
                    "image": image_id,
                    "state_storage": "Linux named volume"
                    if a.interrupt
                    else "Docker Desktop host bind mount",
                    "faults_triggered": sorted(fixture.fired),
                    "results": results,
                },
                indent=2,
            )
        )
    (a.output / "requests.json").write_text(json.dumps(fixture.requests))
    server.shutdown()


if __name__ == "__main__":
    main()
