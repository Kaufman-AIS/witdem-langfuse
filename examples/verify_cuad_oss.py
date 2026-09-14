"""Run the real CUAD app, then verify the same trace in Langfuse and Witdem OSS.

Run with the CUAD application's Python environment. Install this repository's
telemetry extra there first. LANGFUSE_* credentials must be supplied separately.
This writes evidence to --output; it never prints contract text or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import yaml
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from witdem_langfuse.client import Client
from witdem_langfuse.telemetry import configure_langfuse_from_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=["live", "deterministic"], default="deterministic"
    )
    parser.add_argument("--dashboard", default="http://127.0.0.1:28502")
    parser.add_argument("--receiver", default="http://127.0.0.1:24319")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.app.resolve()))
    from contract_review_agent.app.config import Settings
    from contract_review_agent.app.pipeline import run_review

    settings = Settings.from_env(mode=args.mode)
    provider = configure_langfuse_from_env(service_name="haystack-cuad-contract-review")
    captured = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(captured))
    started = datetime.now(UTC) - timedelta(seconds=1)
    origin = args.app.resolve() / ".witdem"
    config = yaml.safe_load((origin / "witdem.yaml").read_text())
    config["telemetry"]["endpoint"] = args.receiver
    for key in ("contracts", "workflows"):
        config[key] = [str(origin / item) for item in config[key]]
    previous = os.environ.get("CONTRACT_REVIEW_WITDEM_CONFIG")
    try:
        with tempfile.TemporaryDirectory(prefix="cuad-langfuse-") as tmp:
            path = Path(tmp) / "witdem.yaml"
            path.write_text(yaml.safe_dump(config))
            os.environ["CONTRACT_REVIEW_WITDEM_CONFIG"] = str(path)
            result = run_review(str(args.source.resolve()), settings)
            provider.force_flush()
    finally:
        if previous is None:
            os.environ.pop("CONTRACT_REVIEW_WITDEM_CONFIG", None)
        else:
            os.environ["CONTRACT_REVIEW_WITDEM_CONFIG"] = previous
    spans = captured.get_finished_spans()
    trace_ids = {f"{s.context.trace_id:032x}" for s in spans}
    if len(trace_ids) != 1:
        raise RuntimeError(
            f"Expected exactly one execution trace, observed {len(trace_ids)}"
        )
    trace_id = trace_ids.pop()
    expected_ids = {f"{s.context.span_id:016x}" for s in spans}
    client = Client(
        os.environ["LANGFUSE_BASE_URL"],
        os.environ["LANGFUSE_PUBLIC_KEY"],
        os.environ["LANGFUSE_SECRET_KEY"],
        os.environ["LANGFUSE_PROJECT_ID"],
        allow_http=os.environ["LANGFUSE_BASE_URL"].startswith("http://127.0.0.1:"),
    )
    end = datetime.now(UTC) + timedelta(seconds=1)
    observations = []
    run = None
    deadline = time.monotonic() + 180
    with httpx.Client(base_url=args.dashboard, timeout=15) as dashboard:
        while time.monotonic() < deadline:
            observations, cursor = client.page(
                started.isoformat(),
                end.isoformat(),
                limit=1000,
                target_kind="trace",
                target_id=trace_id,
            )
            if cursor:
                raise RuntimeError("Acceptance observation bound exceeded")
            response = dashboard.get(
                "/api/v1/runs", params={"page_size": 100, "page": 1}
            )
            response.raise_for_status()
            run = next(
                (r for r in response.json()["items"] if r.get("trace_id") == trace_id),
                None,
            )
            if (
                expected_ids <= {r["id"] for r in observations}
                and run
                and run.get("status") != "running"
            ):
                break
            time.sleep(5)
        if run is None:
            raise RuntimeError(f"Witdem OSS did not expose trace {trace_id}")
        execution_id = run["execution_id"]
        detail = dashboard.get(f"/api/v1/runs/{execution_id}")
        detail.raise_for_status()
        bundle = dashboard.get(f"/api/v1/runs/{execution_id}/evidence-bundle")
        bundle.raise_for_status()
    checks = {
        "all_spans_in_langfuse": expected_ids <= {r["id"] for r in observations},
        "same_trace_in_oss": run["trace_id"] == trace_id,
        "explicit_business_result": run.get("product_goal_reported") is True,
        "contract_identified": run.get("contract_name") == "contract_review",
        "evidence_bundle_available": bool(bundle.json()),
        "application_decision_preserved": run.get("business_outcome")
        == result.get("final_decision"),
        "declared_requirements_present": {
            "The contract review completed",
            "Evidence coverage meets the review threshold",
            "The fictional playbook was evaluated",
            "The review reached a final disposition route",
        }
        <= {item["name"] for item in bundle.json().get("evaluations", [])},
    }
    if args.mode == "live":
        from contract_review_agent.app.showcase import _verification

        checks.update(_verification(run, ["deepseek", "mistral", "openai"])["checks"])
    report = {
        "passed": all(checks.values()),
        "mode": args.mode,
        "checks": checks,
        "trace_id": trace_id,
        "execution_id": execution_id,
        "langfuse_observation_count": len(observations),
        "exported_span_count": len(spans),
        "review_decision": result.get("final_decision"),
        "witdem_run": run,
        "witdem_url": args.dashboard
        + run.get("canonical_url", f"/runs/{execution_id}"),
        "langfuse_url": os.environ["LANGFUSE_BASE_URL"]
        + "/project/"
        + os.environ["LANGFUSE_PROJECT_ID"]
        + "/traces/"
        + trace_id,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for name, data in [
        ("acceptance.json", report),
        ("oss-detail.json", detail.json()),
        ("oss-evidence-bundle.json", bundle.json()),
    ]:
        (args.output / name).write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "witdem_run"}, indent=2))
    if not report["passed"]:
        raise SystemExit("CUAD acceptance checks failed")


if __name__ == "__main__":
    main()
