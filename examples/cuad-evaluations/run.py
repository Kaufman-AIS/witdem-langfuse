"""Publish existing CUAD evaluation records, import traces, and assess via YAML.

Input is an operator-exported original SDK replay page, not generated test scores.
Run the existing CUAD acceptance recipe first to produce real source records.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)

from witdem_langfuse.backfill import Backfill
from witdem_langfuse.client import Client
from witdem_langfuse.evaluations import digest
from witdem_langfuse.replay_delivery import ReplayDelivery
from witdem_langfuse.writeback import publish


def main():
    p = argparse.ArgumentParser(description=__doc__)
    inputs = p.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--records", type=Path)
    inputs.add_argument(
        "--detail", type=Path, help="Existing public Witdem run-detail JSON"
    )
    p.add_argument("--source", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--receiver", required=True)
    p.add_argument("--from-time", required=True)
    p.add_argument("--to-time", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--allow-http", action="store_true")
    a = p.parse_args()
    if a.detail:
        detail = json.loads(a.detail.read_text())
        summary = detail["summary"]
        original = {
            "version": "1.0",
            "source": "application_records",
            "project_id": a.project,
            "source_execution_id": summary["execution_id"],
            "source_trace_id": summary["trace_id"],
            "records": [],
        }
        for r in detail["semantic_records"]:
            if r["attributes"].get("witdem.source") != "sdk":
                continue
            original["records"].append(
                {
                    "version": "1.0",
                    "kind": r["kind"],
                    "name": r["name"],
                    "event_id": r["record_id"],
                    "execution_id": summary["execution_id"],
                    "trace_id": summary["trace_id"],
                    "span_id": None,
                    "value": r["value"],
                    "attributes": r["attributes"],
                }
            )
    else:
        original = json.loads(a.records.read_text())
    if original["project_id"] != a.project:
        raise ValueError("source records project mismatch")
    trace = original["source_trace_id"]
    wanted = {
        "evidence_completeness": "evidence_supported",
        "extraction_confidence": "extraction_trusted",
    }
    bindings = {"version": 1, "requirements": {}}
    scores = []
    for r in original["records"]:
        key = r["attributes"].get("evaluation_key")
        if r["kind"] != "evaluation" or key not in wanted:
            continue
        if wanted[key] in bindings["requirements"]:
            raise ValueError("ambiguous original CUAD evaluation")
        if (
            r["trace_id"] != trace
            or r["execution_id"] != original["source_execution_id"]
        ):
            raise ValueError("original evaluation identity mismatch")
        score_id = "cuad-eval-" + digest(
            [a.project, trace, r["event_id"], r["span_id"]]
        )
        score = {
            "id": score_id,
            "traceId": trace,
            "observationId": r["span_id"],
            "name": key,
            "value": r["value"],
            "dataType": "NUMERIC",
            "comment": "Existing CUAD application evaluation; copied without rerunning a judge.",
            "metadata": {
                "source": "cuad_application_evaluation",
                "original_event_id": r["event_id"],
            },
        }
        if not r["span_id"]:
            score.pop("observationId")
        scores.append(score)
        bindings["requirements"][wanted[key]] = dict(
            evaluation=key,
            name=key,
            source="API",
            subject="observation" if r["span_id"] else "trace",
            score_id=score_id,
            **({"observation_id": r["span_id"]} if r["span_id"] else {}),
        )
    if len(scores) != 2:
        raise ValueError("both real CUAD evaluations required")
    results = [
        r["attributes"]["status"]
        for r in original["records"]
        if r["name"] == "application_outcome" and r["kind"] == "outcome"
    ]
    if len(results) != 1:
        raise ValueError("exactly one explicit application disposition required")
    a.output.mkdir(parents=True, exist_ok=True)
    if a.detail:
        (a.output / "original-summary.json").write_text(
            json.dumps(detail["summary"], indent=2)
        )
    binding_path = a.output / "bindings.yaml"
    binding_path.write_text(yaml.safe_dump(bindings, sort_keys=False))
    (a.output / "published-evaluations.json").write_text(json.dumps(scores, indent=2))
    publish(
        scores,
        base_url=a.source,
        project=a.project,
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        allow_http=a.allow_http,
    )
    source = Client(
        a.source,
        os.environ["LANGFUSE_PUBLIC_KEY"],
        os.environ["LANGFUSE_SECRET_KEY"],
        a.project,
        allow_http=a.allow_http,
    )
    Client(a.receiver, "", "", "", allow_http=a.allow_http)
    headers = {}
    if os.getenv("WITDEM_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["WITDEM_API_KEY"]
    with httpx.Client(timeout=30, follow_redirects=False) as http:
        # Verify asynchronous score readback before freezing a complete snapshot.
        for _ in range(45):
            response = http.get(
                a.source.rstrip("/") + "/api/public/v3/scores",
                params={"traceId": trace, "fields": "details,subject", "limit": 100},
                headers={"Authorization": source.auth},
            )
            response.raise_for_status()
            found = {s["id"] for s in response.json()["data"]}
            if all(s["id"] in found for s in scores):
                break
            time.sleep(2)
        else:
            raise RuntimeError("evaluation readback timed out")

        def send(body):
            response = http.post(
                a.receiver.rstrip("/") + "/v1/traces",
                content=body,
                headers={**headers, "Content-Type": "application/x-protobuf"},
            )
            response.raise_for_status()
            ack = ExportTraceServiceResponse.FromString(response.content)
            if ack.partial_success.rejected_spans or ack.partial_success.error_message:
                raise RuntimeError("partial trace acceptance")

        backfill = Backfill(
            a.output / "traces-context.sqlite",
            source,
            a.receiver,
            a.from_time,
            a.to_time,
            send=send,
            workflow_context=True,
        )
        result = backfill.run(1)
        (a.output / "backfill-first-page.json").write_text(json.dumps(result))
        if not result["complete"]:
            result = backfill.run(10)
        if not result["complete"]:
            raise RuntimeError("trace interval exceeds demo budget; rerun to resume")
    # Reuse the existing authored workflow and lifecycle only. Do not import
    # original goal/requirement records: those must come from Langfuse evaluations.
    workflow_page = {
        **original,
        "records": [
            r
            for r in original["records"]
            if r["name"]
            in ("workflow.definition", "execution.started", "execution.completed")
        ],
    }
    workflow_manifest = a.output / "workflow.jsonl"
    workflow_manifest.write_text(json.dumps(workflow_page) + "\n")
    with httpx.Client(timeout=30, follow_redirects=False) as http:

        def send_record(body):
            response = http.post(
                a.receiver.rstrip("/") + "/sdk/v1/records",
                content=body,
                headers={**headers, "Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()

        ReplayDelivery(
            workflow_manifest,
            a.output / "workflow.sqlite",
            a.receiver,
            send=send_record,
        ).run()
    command = [
        sys.executable,
        "-m",
        "witdem_langfuse.evaluations",
        "--contract",
        str(Path(__file__).with_name("contract.yaml")),
        "--bindings",
        str(binding_path),
        "--source",
        a.source,
        "--project",
        a.project,
        "--trace",
        trace,
        "--from-time",
        a.from_time,
        "--to-time",
        a.to_time,
        "--workspace",
        str(a.output / "assessment"),
        "--receiver",
        a.receiver,
        "--application-result",
        results[0],
    ]
    if a.allow_http:
        command.append("--allow-http")
    subprocess.run(command + ["--max-records", "2"], check=True)
    subprocess.run(command + ["--offline"], check=True)
    (a.output / "reassess-command.json").write_text(
        json.dumps(command + ["--offline"], indent=2)
    )


if __name__ == "__main__":
    main()
