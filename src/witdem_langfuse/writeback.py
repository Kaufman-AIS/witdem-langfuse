"""Publish explicit Witdem assessments using Langfuse's existing score API."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .backfill import identity
from .client import Client


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def assessment_scores(detail, *, project, trace_id, evidence_url):
    parsed = urlsplit(evidence_url)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or not (
            parsed.scheme == "https"
            or parsed.scheme == "http"
            and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        )
    ):
        raise ValueError(
            "evidence URL must be HTTPS or local HTTP without credentials/query/fragment"
        )
    summary = detail["summary"]
    if summary.get("trace_id") not in (
        trace_id,
        identity(project, "trace", trace_id, 16).hex(),
    ):
        raise ValueError("Witdem execution does not match the Langfuse trace binding")
    goal = detail.get("outcomes", {}).get("product_goal")
    if (
        not isinstance(goal, dict)
        or goal.get("witdem.source") != "sdk"
        or summary.get("product_goal_reported") is not True
    ):
        raise ValueError(
            "writeback requires an explicit application-reported Witdem assessment"
        )
    if not goal.get("contract_hash") or not goal.get("contract_version"):
        raise ValueError("writeback requires a versioned outcome contract")
    derived = goal.get("assessment_source") == "langfuse_evaluations"
    facts = {
        (
            "contract_requirements_met" if derived else "business_outcome_achieved"
        ): goal.get("product_goal_achieved"),
        "evidence_sufficient": goal.get("decision_evidence_sufficient"),
    }
    result = detail["outcomes"].get("business")
    snapshot = {
        "execution_id": summary["execution_id"],
        "trace_id": trace_id,
        "contract_hash": goal["contract_hash"],
        "contract_version": goal["contract_version"],
        "facts": facts,
        "source_assessment_sha256": goal.get("assessment_sha256"),
        "business_result": result,
        "failed_requirement_ids": goal.get("failed_requirement_ids", []),
        "unknown_requirement_ids": goal.get("unknown_requirement_ids", []),
    }
    fingerprint = digest(snapshot)
    metadata = {
        "source": "witdem",
        "assessment_source": goal.get("assessment_source", "application_report"),
        "assessment_sha256": fingerprint,
        "execution_id": summary["execution_id"],
        "contract_name": goal.get("contract_name"),
        "contract_hash": goal["contract_hash"],
        "contract_version": goal["contract_version"],
        "evidence_url": evidence_url,
        "contract_reference": str(goal.get("contract_name") or "contract")
        + "@"
        + str(goal["contract_version"]),
        "failed_requirement_ids": snapshot["failed_requirement_ids"],
        "unknown_requirement_ids": snapshot["unknown_requirement_ids"],
    }
    scores = []

    def add(name, value, data_type):
        scores.append(
            {
                "id": "witdem-" + digest([project, trace_id, fingerprint, name]),
                "traceId": trace_id,
                "name": name,
                "value": value,
                "dataType": data_type,
                "metadata": metadata.copy(),
                "comment": "Reported by Witdem; contract "
                + str(goal["contract_version"])
                + ". Evidence: "
                + evidence_url,
            }
        )

    for name, value in facts.items():
        if value is None:
            continue  # Unknown is not false.
        if type(value) is not bool:
            raise ValueError("business facts must be explicit booleans or null")
        add(name, 1.0 if value else 0.0, "BOOLEAN")
    if derived:
        status = (
            "met"
            if goal.get("product_goal_achieved") is True
            else "failed"
            if goal.get("product_goal_achieved") is False
            else "unknown"
        )
        add("contract_assessment_status", status, "CATEGORICAL")
    if result is not None:
        if not isinstance(result, str) or not result or len(result) > 500:
            raise ValueError("invalid explicit business result")
        add("business_result", result, "CATEGORICAL")
    return scores


def publish(scores, *, base_url, project, public_key, secret_key, allow_http=False):
    Client(base_url, public_key, secret_key, project, allow_http=allow_http)
    trace_ids = {score["traceId"] for score in scores}
    if len(trace_ids) != 1:
        raise ValueError("publish one nonempty assessment at a time")
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        auth=(public_key, secret_key),
        timeout=30,
        follow_redirects=False,
    ) as client:
        response = client.get(
            "/api/public/v2/observations",
            params={"traceId": next(iter(trace_ids)), "limit": 1, "fields": "core"},
        )
        response.raise_for_status()
        rows = response.json().get("data", [])
        if not rows or any(
            row.get("projectId") != project or row.get("traceId") not in trace_ids
            for row in rows
        ):
            raise ValueError("target project/trace not verified; no scores published")
        acknowledgements = []
        for score in scores:
            response = client.post("/api/public/scores", json=score)
            response.raise_for_status()
            result = response.json()
            if result.get("id") != score["id"]:
                raise ValueError("unexpected score acknowledgement")
            acknowledgements.append(result)
        return acknowledgements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detail", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--evidence-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--allow-http", action="store_true")
    args = parser.parse_args()
    scores = assessment_scores(
        json.loads(args.detail.read_text()),
        project=args.project,
        trace_id=args.trace,
        evidence_url=args.evidence_url,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(scores, indent=2) + "\n")
    if args.publish:
        publish(
            scores,
            base_url=os.environ["LANGFUSE_BASE_URL"],
            project=args.project,
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            allow_http=args.allow_http,
        )
    print(
        json.dumps(
            {
                "scores": len(scores),
                "published": args.publish,
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
