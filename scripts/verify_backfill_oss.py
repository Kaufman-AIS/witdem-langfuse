"""Verify an imported CUAD run against its original instrumented acceptance report.

Reads an existing OSS deployment only. It does not alter OSS code or data.
"""

import argparse
import json
from pathlib import Path

import httpx

from witdem_langfuse.backfill import identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", default="http://127.0.0.1:28512")
    parser.add_argument("--project", required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = json.loads(args.original.read_text())
    rows = json.loads(args.observations.read_text())["data"]
    source_trace = original["trace_id"]
    assert rows and all(
        row["projectId"] == args.project and row["traceId"] == source_trace
        for row in rows
    )
    trace = identity(args.project, "trace", source_trace, 16).hex()
    with httpx.Client(base_url=args.dashboard, timeout=15) as client:
        response = client.get(f"/api/v1/runs/{trace}")
        response.raise_for_status()
        detail = response.json()
        response = client.get(f"/api/v1/runs/{trace}/evidence-bundle")
        response.raise_for_status()
        bundle = response.json()
    summary = detail["summary"]
    operations = bundle["operations"]
    expected_parents = {
        row["id"]: identity(
            args.project, source_trace, row["parentObservationId"], 8
        ).hex()
        if row.get("parentObservationId")
        else None
        for row in rows
    }
    checks = {
        "observation_count": len(operations) == len(rows),
        "original_identities": {
            o["attributes"]["langfuse.observation.id"] for o in operations
        }
        == {r["id"] for r in rows},
        "parent_relationships": all(
            (o.get("parent_span_id") or None)
            == expected_parents[o["attributes"]["langfuse.observation.id"]]
            for o in operations
        ),
        "model_calls_match_original": summary["model_calls"]
        == original["witdem_run"]["model_calls"],
        "token_total_matches_original": summary["total_tokens"]
        == original["witdem_run"]["total_tokens"],
        "no_business_truth_inferred": summary.get("product_goal_reported") is False
        and summary["application_outcome"] is None
        and not bundle["outcomes"],
    }
    report = {
        "passed": all(checks.values()),
        "checks": checks,
        "imported_trace_id": trace,
        "source_trace_id": source_trace,
        "operations": len(operations),
        "links": len(bundle["links"]),
        "model_calls": summary["model_calls"],
        "tokens": summary["total_tokens"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit("OSS backfill verification failed")


if __name__ == "__main__":
    main()
