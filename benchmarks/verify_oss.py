"""Verify projected OSS counts after synthetic benchmark ingestion."""

import argparse
import json
import time
from pathlib import Path

import httpx

from witdem_langfuse.backfill import identity


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--dashboard", default="http://127.0.0.1:28522")
    p.add_argument("--timeout", type=float, default=300)
    a = p.parse_args()
    report = json.loads((a.output / "results.json").read_text())
    checks = []
    for case in report["results"]:
        root = a.output / str(case["observations"]) / "state/assessment"
        page = json.loads((root / "assessment.jsonl").read_text())
        execution = identity(
            page["project_id"], "trace", page["source_trace_id"], 16
        ).hex()
        started = time.monotonic()
        deadline = started + a.timeout
        while True:
            response = httpx.get(a.dashboard + "/api/v1/runs/" + execution, timeout=60)
            if response.status_code == 200:
                detail = response.json()
                summary = detail["summary"]
                if (
                    summary["operation_count"] == case["observations"]
                    and summary["product_goal_reported"]
                ):
                    break
            elif response.status_code not in (404, 503):
                response.raise_for_status()
            if time.monotonic() > deadline:
                raise RuntimeError("OSS projection did not reach expected counts")
            time.sleep(2)
        assert summary["product_goal_achieved"] is True
        assert summary["application_outcome"] == "approved_with_exceptions"
        requirements = [
            r
            for r in detail["evaluation_results"]
            if r["attributes"].get("requirement_id")
        ]
        assert len(requirements) == 2
        for r in requirements:
            assert r["attributes"]["passed"] is True
            scores = r["attributes"]["langfuse_scores"]
            assert len(scores) == 1 and scores[0] in page["scores"]
        assert len(detail["evaluation_results"]) == 4
        checks.append(
            {
                "observations": case["observations"],
                "operation_count": summary["operation_count"],
                "requirement_count": 2,
                "evaluation_count": 4,
                "achieved": True,
                "application_outcome": summary["application_outcome"],
                "verification_wait_seconds": round(time.monotonic() - started, 3),
            }
        )
        print(json.dumps(checks[-1]), flush=True)
    (a.output / "oss-verification.json").write_text(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()
