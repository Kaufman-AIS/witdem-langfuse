"""Verify the public OSS evidence and record a short terminal walkthrough."""

import argparse
import json
import math
import time
from pathlib import Path

import httpx

from witdem_langfuse.backfill import identity
from witdem_langfuse.evaluations import assess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--dashboard", required=True)
    p.add_argument(
        "--live-only",
        action="store_true",
        help="verify a fresh live import without synthetic fixtures",
    )
    a = p.parse_args()
    recording = [
        json.dumps(
            {
                "version": 2,
                "width": 110,
                "height": 28,
                "timestamp": int(time.time()),
                "title": "Witdem for Langfuse: existing evaluations to business requirements",
            }
        )
    ]
    at = 0

    def say(text):
        nonlocal at
        print(text, flush=True)
        recording.append(json.dumps([at, "o", text + "\r\n"]))
        at += 2

    checks = {}
    for label in ["live"] if a.live_only else ["live", "failed", "missing"]:
        root = a.output / label
        manifest = (
            root / "assessment/assessment.jsonl"
            if label == "live"
            else root / "assessment.jsonl"
        )
        page = json.loads(manifest.read_text())
        expected = assess(page)
        execution = identity(
            page["project_id"], "trace", page["source_trace_id"], 16
        ).hex()
        url = a.dashboard.rstrip("/") + "/api/v1/runs/" + execution
        for _ in range(45):
            r = httpx.get(url, timeout=30)
            if r.status_code == 200:
                detail = r.json()
                if detail["summary"].get("product_goal_reported"):
                    break
            time.sleep(1)
        else:
            raise RuntimeError("OSS projection did not become available")
        summary = detail["summary"]
        assert summary["product_goal_achieved"] is expected["achieved"]
        assert summary["application_outcome"] == page["application_result"]
        evaluations = detail["evaluation_results"]
        required = [r for r in evaluations if r["attributes"].get("requirement_id")]
        assert len(required) == 2, "Duplicate or missing requirements"
        for req in expected["requirements"]:
            observed = next(
                r
                for r in required
                if r["attributes"]["requirement_id"] == req["requirement_id"]
            )
            assert observed["attributes"]["passed"] is req["passed"]
            assert observed["attributes"]["langfuse_scores"] == req["scores"]
        bundle = httpx.get(url + "/evidence-bundle", timeout=30)
        bundle.raise_for_status()
        (root / "oss-detail.json").write_text(json.dumps(detail, indent=2))
        (root / "oss-evidence-bundle.json").write_text(bundle.text)
        if label == "live":
            reference = root / "original-summary.json"
            if reference.exists():
                original = json.loads(reference.read_text())
                for field in (
                    "model_calls",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "providers",
                    "models",
                ):
                    assert summary[field] == original[field], (
                        f"Imported {field} differs from source"
                    )
                for field in ("measured_cost", "cost_coverage"):
                    if original.get(field) is not None:
                        assert summary.get(field) is not None and math.isclose(
                            summary[field], original[field], rel_tol=1e-9
                        ), f"Imported {field} differs from source"
                say(
                    f"Usage: {summary['model_calls']} model calls; {summary['total_tokens']} tokens; cost {summary['measured_cost']}"
                )
                say("Providers: " + str(summary.get("providers")))
            assert expected["trace_url"] in bundle.text
        assert detail["workflow_replay"] is not None
        say(
            "REAL CUAD RUN"
            if label == "live"
            else "SYNTHETIC FIXTURE: " + label.upper()
        )
        say("Goal: " + page["contract"]["goal"]["name"])
        for req in expected["requirements"]:
            name = page["contract"]["goal"]["requirements"][req["requirement_id"]][
                "name"
            ]
            status = (
                "MET"
                if req["passed"] is True
                else "FAILED"
                if req["passed"] is False
                else "UNKNOWN"
            )
            say(f"  {status}: {name} (value={req['value']}, target={req['target']})")
        say("Application disposition stays separate: " + page["application_result"])
        say(
            "Existing Witdem UI: "
            + a.dashboard
            + "/workflows/contract-review/executions/"
            + execution
            + "?view=goals"
        )
        checks[label] = {
            "passed": True,
            "execution_id": execution,
            "requirements": 2,
            "outcome": expected["achieved"],
        }
    say("No agent or judge was called to assess these saved evaluations.")
    (a.output / "verification.json").write_text(json.dumps(checks, indent=2))
    (a.output / "walkthrough.cast").write_text("\n".join(recording) + "\n")


if __name__ == "__main__":
    main()
