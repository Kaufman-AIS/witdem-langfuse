"""Clearly labeled synthetic failed/missing cases in the existing OSS UI.

These are saved evaluation fixtures, not claims of real model performance.
Their provenance is labeled synthetic, and they do not publish to Langfuse.
"""

import argparse
import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from witdem_langfuse.backfill import duckle_encode, identity
from witdem_langfuse.evaluations import assess, duckle_assessment
from witdem_langfuse.replay_delivery import ReplayDelivery


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", type=Path, required=True)
    p.add_argument("--receiver", required=True)
    a = p.parse_args()
    if urlsplit(a.receiver).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("synthetic UI fixtures are restricted to a local receiver")
    live = json.loads((a.live / "assessment/assessment.jsonl").read_text())
    workflow = json.loads((a.live / "workflow.jsonl").read_text())
    with httpx.Client(timeout=30, follow_redirects=False) as http:

        def send(body):
            r = http.post(
                a.receiver.rstrip("/") + "/sdk/v1/records",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            return r.json()

        for label in ["failed", "missing"]:
            page = copy.deepcopy(live)
            trace = identity("fixture", label, live["source_trace_id"], 16).hex()
            span = "abcdef0123456789"
            root = a.live.parent / label
            root.mkdir(parents=True, exist_ok=True)
            page["source_trace_id"] = trace
            page["fixture"] = label
            page["contract"]["name"] = "SYNTHETIC FIXTURE: " + label
            page["contract"]["goal"]["description"] = (
                "SYNTHETIC FIXTURE, not a real review. "
                + page["contract"]["goal"]["description"]
            )
            chosen = []
            for requirement, b in page["bindings"]["requirements"].items():
                s = next(
                    copy.deepcopy(s) for s in live["scores"] if s["id"] == b["score_id"]
                )
                s["subject"] = {"kind": "observation", "id": span, "traceId": trace}
                s["metadata"] = {"source": "synthetic_fixture"}
                s["comment"] = (
                    "SYNTHETIC FIXTURE: "
                    + label
                    + " evaluation; not produced by a judge."
                )
                s["id"] = "fixture-" + label + "-" + s["name"]
                b["score_id"] = s["id"]
                b["observation_id"] = span
                b["subject"] = "observation"
                if requirement == "evidence_supported":
                    if label == "missing":
                        continue
                    s["value"] = 0.6
                chosen.append(s)
            page["scores"] = chosen
            observation = {
                "id": span,
                "traceId": trace,
                "projectId": page["project_id"],
                "name": "CUAD SYNTHETIC FIXTURE: " + label,
                "type": "SPAN",
                "startTime": "2026-09-14T10:00:00Z",
                "endTime": "2026-09-14T10:00:01Z",
                "metadata": {"attributes.witdem.workflow.id": "contract-review"},
            }
            r = http.post(
                a.receiver.rstrip("/") + "/v1/traces",
                content=duckle_encode([observation], page["project_id"]),
                headers={"Content-Type": "application/x-protobuf"},
            )
            r.raise_for_status()
            wf = copy.deepcopy(workflow)
            wf["source_trace_id"] = trace
            wf["source_execution_id"] = trace
            for record in wf["records"]:
                record["execution_id"] = trace
                record["trace_id"] = trace
                record["span_id"] = span
                record["event_id"] = "fixture-" + label + "-" + record["event_id"]
            wpath = root / "workflow.jsonl"
            wpath.write_text(json.dumps(wf) + "\n")
            ReplayDelivery(wpath, root / "workflow.sqlite", a.receiver, send=send).run()
            path = root / "assessment.jsonl"
            path.write_text(json.dumps(page) + "\n")
            ReplayDelivery(
                path,
                root / "delivery.sqlite",
                a.receiver,
                send=send,
                transform=duckle_assessment,
            ).run()
            report = assess(page)
            report["execution_id"] = identity(
                page["project_id"], "trace", trace, 16
            ).hex()
            (root / "assessment.json").write_text(json.dumps(report, indent=2))
            print(label, report["execution_id"], report["achieved"])


if __name__ == "__main__":
    main()
