"""Contract assessments from explicitly bound Langfuse scores; no judge calls."""

import argparse
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from .backfill import timestamp
from .client import Client
from .replay import remap
from .replay_delivery import ReplayDelivery, canonical, duckle_page


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evaluation: str
    name: str
    source: Literal["API", "EVAL", "ANNOTATION"]
    subject: Literal["trace", "observation"]
    score_id: str | None = None
    config_id: str | None = None
    observation_id: str | None = None


def validate(contract, bindings):
    # Use the existing SDK's descriptive contract schema, not another contract DSL.
    from witdem_sdk._contract import DescriptiveContractSpec

    spec = DescriptiveContractSpec.model_validate(contract)
    if bindings.get("version") != 1 or set(bindings) != {"version", "requirements"}:
        raise ValueError("bindings require version 1 and requirements")
    if set(bindings["requirements"]) != set(spec.goal.requirements):
        raise ValueError("bind every declared requirement exactly once")
    for raw in bindings["requirements"].values():
        b = Binding.model_validate(raw)
        if bool(b.score_id) == bool(b.config_id):
            raise ValueError("choose exactly one score_id or config_id")
        if (b.subject == "observation") != bool(b.observation_id):
            raise ValueError("observation scope requires its exact observation_id")
        e = spec.evaluations.get(b.evaluation)
        if e is None or not (
            type(e.target) is bool
            or type(e.target) in (float, int)
            and math.isfinite(e.target)
        ):
            raise ValueError("evaluation requires a Boolean or finite numeric target")
        if type(e.target) is not bool and e.direction is None:
            raise ValueError("numeric targets require direction")
    return spec


def own_score(s):
    return (
        str(s.get("id", "")).startswith("witdem-")
        or str((s.get("metadata") or {}).get("source", "")).lower() == "witdem"
    )


def assess(page):
    spec = validate(page["contract"], page["bindings"])
    project, trace = page["project_id"], page["source_trace_id"]
    Client(
        page["source_url"], "", "", project, allow_http=page.get("allow_http", False)
    )
    trace_url = (
        page["source_url"].rstrip("/")
        + "/project/"
        + quote(project, safe="")
        + "/traces/"
        + quote(trace, safe="")
    )
    if page.get("fixture"):
        trace_url = None  # Synthetic fixtures have no upstream Langfuse trace.
    rows = {}
    for s in page["scores"]:
        if s.get("projectId") != project:
            raise ValueError("score project does not match the bound project")
        if not s.get("id"):
            raise ValueError("score identity required")
        if s["id"] in rows and canonical(rows[s["id"]]) != canonical(s):
            raise ValueError("conflicting snapshots of the same score")
        rows[s["id"]] = s
    results = []
    for key, raw in page["bindings"]["requirements"].items():
        b = Binding.model_validate(raw)
        matches = []
        for s in rows.values():
            subject = s.get("subject") or {}
            if own_score(s) or s.get("name") != b.name or s.get("source") != b.source:
                continue
            if (
                b.score_id
                and s["id"] != b.score_id
                or b.config_id
                and s.get("configId") != b.config_id
            ):
                continue
            if subject.get("kind") != b.subject:
                continue
            if b.subject == "trace" and subject.get("id") != trace:
                continue
            if b.subject == "observation" and (
                subject.get("traceId") != trace or subject.get("id") != b.observation_id
            ):
                continue
            matches.append(s)
        e = spec.evaluations[b.evaluation]
        passed, reason, value = None, "missing_evaluation", None
        if len(matches) > 1:
            reason = "ambiguous_evaluation"
        elif len(matches) == 1:
            s = matches[0]
            value = s.get("value")
            valid = (
                type(e.target) is bool
                and s.get("dataType") == "BOOLEAN"
                and type(value) is bool
            ) or (
                type(e.target) is not bool
                and s.get("dataType") == "NUMERIC"
                and type(value) in (int, float)
                and math.isfinite(value)
            )
            if valid:
                passed = (
                    value == e.target
                    if type(e.target) is bool
                    else value >= e.target
                    if e.direction == "higher_is_better"
                    else value <= e.target
                )
                reason = "met" if passed else "threshold_failed"
            else:
                reason = "invalid_evaluation_value"
        results.append(
            {
                "requirement_id": key,
                "evaluation": b.evaluation,
                "passed": passed,
                "reason": reason,
                "value": value,
                "target": e.target,
                "scores": matches,
                "trace_url": trace_url,
            }
        )
    failed = [r["requirement_id"] for r in results if r["passed"] is False]
    unknown = [r["requirement_id"] for r in results if r["passed"] is None]
    achieved = False if failed else None if unknown else True
    return {
        "requirements": results,
        "achieved": achieved,
        "failed": failed,
        "unknown": unknown,
        "trace_url": trace_url,
    }


def records(page):
    from witdem_sdk._contract import WitdemProjectConfig, contract_definition

    spec = validate(page["contract"], page["bindings"])
    assessment = assess(page)
    config = WitdemProjectConfig(
        version=2, service={"name": "witdem-langfuse"}, contracts={spec.id: spec}
    )
    contract_hash, definition = contract_definition(config, spec.id, spec)
    fingerprint = digest(
        [
            page["contract"],
            page["bindings"],
            sorted(
                (s for s in page["scores"] if not own_score(s)), key=lambda s: s["id"]
            ),
            page.get("application_result"),
        ]
    )
    shared = {
        "contract_name": spec.id,
        "contract_hash": contract_hash,
        "contract_version": "2.0",
        "product_goal_name": spec.goal.name,
        "product_goal_description": spec.goal.description,
        "result_name": spec.result.name,
        "assessment_source": "langfuse_evaluations",
        "assessment_sha256": fingerprint,
        "langfuse_trace_url": assessment["trace_url"],
    }
    output = []

    def add(kind, name, value=None, attributes=None, span=None):
        output.append(
            {
                "version": "1.0",
                "kind": kind,
                "name": name,
                "value": value,
                "attributes": {**shared, **(attributes or {})},
                "event_id": digest(
                    [
                        page["project_id"],
                        page["source_trace_id"],
                        fingerprint,
                        kind,
                        name,
                    ]
                ),
                "execution_id": page["source_trace_id"],
                "trace_id": page["source_trace_id"],
                "span_id": span,
            }
        )

    add("event", "contract.definition", attributes=definition)
    for r in assessment["requirements"]:
        req = spec.goal.requirements[r["requirement_id"]]
        s = r["scores"][0] if len(r["scores"]) == 1 else None
        span = (
            s["subject"]["id"] if s and s["subject"]["kind"] == "observation" else None
        )
        attrs = {
            "passed": r["passed"],
            "label": "passed"
            if r["passed"] is True
            else "failed"
            if r["passed"] is False
            else "unknown",
            "requirement_id": r["requirement_id"],
            "evaluation_key": "goal_requirement." + r["requirement_id"],
            "requirement_failure_label": req.failure.label,
            "requirement_failure_description": req.failure.description,
            "evaluation_description": req.description,
            "evidence_reason": r["reason"],
            "langfuse_scores": r["scores"],
            "target": r["target"],
        }
        add("evaluation", req.name, r["passed"], attrs, span)
        if s:
            e = spec.evaluations[r["evaluation"]]
            add(
                "evaluation",
                e.name,
                r["value"],
                {
                    "evaluation_key": r["evaluation"],
                    "score": r["value"] if type(r["value"]) is not bool else None,
                    "target": e.target,
                    "direction": e.direction,
                    "unit": e.unit,
                    "langfuse_scores": r["scores"],
                },
                span,
            )
    if page.get("application_result") is not None:
        # This value is supplied separately from the original application, never inferred.
        add(
            "outcome",
            "application_outcome",
            attributes={
                "status": page["application_result"],
                "assessment_source": "application_report",
            },
        )
    add(
        "outcome",
        "product_goal",
        attributes={
            "status": "achieved"
            if assessment["achieved"] is True
            else "failed"
            if assessment["achieved"] is False
            else "unknown",
            "product_goal_achieved": assessment["achieved"],
            "failed_requirement_ids": assessment["failed"],
            "unknown_requirement_ids": assessment["unknown"],
            "decision_evidence_sufficient": None,
            "closest_blocker": (assessment["failed"] + assessment["unknown"] + [None])[
                0
            ],
        },
    )
    return remap(
        {
            "version": "1.0",
            "source": "application_records",
            "project_id": page["project_id"],
            "source_execution_id": page["source_trace_id"],
            "source_trace_id": page["source_trace_id"],
            "records": output,
        }
    )


def process(row):
    return {"records_json": canonical(records(json.loads(row["page_json"])))}


def duckle_assessment(page):
    return duckle_page(
        page, pipeline_name="evaluations.pipeline.json", validate_page=False
    )


class ScoreSnapshot:
    """Persist each fetched page and cursor atomically before assessing a trace."""

    def __init__(self, path, config, fetch):
        self.path, self.config, self.fetch = Path(path), config, fetch

    def run(self, max_pages=10):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(self.path) as db:
                os.chmod(self.path, 0o600)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS state(config TEXT, cursor TEXT, complete INTEGER)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS scores(id TEXT PRIMARY KEY, body TEXT)"
                )
                saved = db.execute("SELECT * FROM state").fetchone()
                if not saved:
                    db.execute(
                        "INSERT INTO state VALUES(?,NULL,0)", (canonical(self.config),)
                    )
                    db.commit()
                    saved = (canonical(self.config), None, 0)
                if saved[0] != canonical(self.config):
                    raise ValueError("score checkpoint configuration changed")
                cursor, complete = saved[1:]
                for _ in range(max_pages):
                    if complete:
                        break
                    response = self.fetch(cursor)
                    for s in response["data"]:
                        if s.get("projectId") != self.config["project"]:
                            raise ValueError("score project mismatch")
                        body = canonical(s)
                        old = db.execute(
                            "SELECT body FROM scores WHERE id=?", (s["id"],)
                        ).fetchone()
                        if old and old[0] != body:
                            raise ValueError(
                                "source score changed during snapshot; start a fresh checkpoint"
                            )
                        db.execute(
                            "INSERT OR IGNORE INTO scores VALUES(?,?)", (s["id"], body)
                        )
                    following = response.get("meta", {}).get("cursor")
                    if following and following == cursor:
                        raise ValueError("score cursor did not advance")
                    cursor, complete = following, not following
                    db.execute(
                        "UPDATE state SET cursor=?,complete=?", (cursor, int(complete))
                    )
                    db.commit()
                return bool(complete), [
                    json.loads(r[0])
                    for r in db.execute("SELECT body FROM scores ORDER BY id")
                ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--from-time", required=True, help="Inclusive score timestamp")
    parser.add_argument(
        "--to-time",
        required=True,
        help="Exclusive score timestamp; include delayed evaluations",
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--receiver")
    parser.add_argument(
        "--application-result", help="Original application-reported disposition only"
    )
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-records", type=int, default=100)
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Reassess the saved complete snapshot without source access",
    )
    args = parser.parse_args()
    if timestamp(args.from_time) >= timestamp(args.to_time) or args.max_pages < 1:
        raise ValueError("positive page budget and ordered timestamp bounds required")
    contract = yaml.safe_load(args.contract.read_text())
    bindings = yaml.safe_load(args.bindings.read_text())
    validate(contract, bindings)
    args.workspace.mkdir(parents=True, exist_ok=True)
    source = Client(
        args.source,
        os.getenv("LANGFUSE_PUBLIC_KEY", ""),
        os.getenv("LANGFUSE_SECRET_KEY", ""),
        args.project,
        allow_http=args.allow_http,
    )
    config = {
        "source": args.source,
        "project": args.project,
        "trace": args.trace,
        "start": args.from_time,
        "end": args.to_time,
    }
    with httpx.Client(timeout=30, follow_redirects=False) as http:

        def fetch(cursor):
            if args.offline:
                raise ValueError("offline mode requires a completed score snapshot")
            params = {
                "traceId": args.trace,
                "fromTimestamp": args.from_time,
                "toTimestamp": args.to_time,
                "limit": 100,
                "fields": "details,subject,annotation",
            }
            if cursor:
                params["cursor"] = cursor
            response = http.get(
                args.source.rstrip("/") + "/api/public/v3/scores",
                params=params,
                headers={"Authorization": source.auth},
            )
            response.raise_for_status()
            if len(response.content) > 16 * 1024 * 1024:
                raise ValueError("score page exceeds 16 MiB")
            return response.json()

        complete, scores = ScoreSnapshot(
            args.workspace / "scores.sqlite", config, fetch
        ).run(args.max_pages)
        if not complete:
            print(canonical({"complete": False, "scores": len(scores)}))
            return
        page = {
            "project_id": args.project,
            "source_trace_id": args.trace,
            "source_url": args.source,
            "allow_http": args.allow_http,
            "contract": contract,
            "bindings": bindings,
            "scores": scores,
            "application_result": args.application_result,
        }
        manifest = args.workspace / "assessment.jsonl"
        body = canonical(page) + "\n"
        if manifest.exists() and manifest.read_text() != body:
            raise ValueError(
                "assessment workspace is immutable; choose a new workspace"
            )
        manifest.write_text(body)
        transformed = duckle_assessment(page)
        (args.workspace / "records.json").write_text(json.dumps(transformed, indent=2))
        result = assess(page)
        (args.workspace / "assessment.json").write_text(json.dumps(result, indent=2))
        if args.receiver:
            Client(args.receiver, "", "", "", allow_http=args.allow_http)
            headers = {"Content-Type": "application/json"}
            if os.getenv("WITDEM_API_KEY"):
                headers["Authorization"] = "Bearer " + os.environ["WITDEM_API_KEY"]

            def send(body):
                response = http.post(
                    args.receiver.rstrip("/") + "/sdk/v1/records",
                    content=body,
                    headers=headers,
                )
                response.raise_for_status()
                return response.json()

            delivery = ReplayDelivery(
                manifest,
                args.workspace / "delivery.sqlite",
                args.receiver,
                send=send,
                transform=duckle_assessment,
            ).run(args.max_records)
            result["delivery"] = delivery
        print(
            canonical(
                {
                    "achieved": result["achieved"],
                    "failed": result["failed"],
                    "unknown": result["unknown"],
                    "delivery": result.get("delivery"),
                }
            )
        )


if __name__ == "__main__":
    main()
