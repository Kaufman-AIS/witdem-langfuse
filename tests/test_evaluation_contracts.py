import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from witdem_langfuse.evaluations import (
    ScoreSnapshot,
    assess,
    duckle_assessment,
    records,
)
from witdem_langfuse.replay_delivery import ReplayDelivery

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    contract = yaml.safe_load(
        (ROOT / "examples/cuad-evaluations/contract.yaml").read_text()
    )
    bindings = {"version": 1, "requirements": {}}
    scores = []
    for i, (key, evaluation) in enumerate(
        zip(contract["goal"]["requirements"], contract["evaluations"])
    ):
        bindings["requirements"][key] = {
            "evaluation": evaluation,
            "name": evaluation,
            "source": "EVAL",
            "subject": "observation",
            "config_id": "cfg-" + evaluation,
            "observation_id": "0123456789abcdef",
        }
        scores.append(
            {
                "id": str(i),
                "projectId": "p",
                "name": evaluation,
                "value": 0.9,
                "dataType": "NUMERIC",
                "source": "EVAL",
                "configId": "cfg-" + evaluation,
                "comment": "Existing evaluator reasoning",
                "subject": {
                    "kind": "observation",
                    "id": "0123456789abcdef",
                    "traceId": "a" * 32,
                },
            }
        )
    return {
        "project_id": "p",
        "source_trace_id": "a" * 32,
        "source_url": "https://cloud.langfuse.com",
        "contract": contract,
        "bindings": bindings,
        "scores": scores,
        "application_result": "approved_with_exceptions",
    }


class EvaluationContracts(unittest.TestCase):
    def test_threshold_and_unknown_are_distinct(self):
        p = fixture()
        self.assertIs(assess(p)["achieved"], True)
        p["scores"][0]["value"] = 0.79
        self.assertEqual(assess(p)["failed"], ["evidence_supported"])
        p["scores"] = p["scores"][1:]
        self.assertIsNone(assess(p)["achieved"])
        self.assertEqual(assess(p)["unknown"], ["evidence_supported"])

    def test_identity_scope_and_circular_scores(self):
        for field, wrong in [
            ("source", "API"),
            ("configId", "different"),
            ("name", "different"),
        ]:
            p = fixture()
            p["scores"][0][field] = wrong
            self.assertEqual(assess(p)["unknown"], ["evidence_supported"])
        for field in ["id", "traceId"]:
            p = fixture()
            p["scores"][0]["subject"][field] = "other"
            self.assertEqual(assess(p)["unknown"], ["evidence_supported"])
        p = fixture()
        p["scores"][0]["metadata"] = {"source": "witdem"}
        self.assertEqual(assess(p)["unknown"], ["evidence_supported"])
        p = fixture()
        p["scores"][0]["projectId"] = "other"
        with self.assertRaises(ValueError):
            assess(p)

    def test_ambiguous_and_invalid(self):
        p = fixture()
        duplicate = copy.deepcopy(p["scores"][0])
        duplicate["id"] = "third"
        p["scores"].append(duplicate)
        self.assertEqual(assess(p)["requirements"][0]["reason"], "ambiguous_evaluation")
        for value in [True, "0.9", None, float("nan")]:
            p = fixture()
            p["scores"][0]["value"] = value
            self.assertIsNone(assess(p)["requirements"][0]["passed"])

    def test_boolean_target(self):
        p = fixture()
        e = p["contract"]["evaluations"]["evidence_completeness"]
        e["target"] = True
        e.pop("direction")
        p["scores"][0].update(dataType="BOOLEAN", value=False)
        self.assertFalse(assess(p)["requirements"][0]["passed"])
        p["scores"][0]["value"] = 1
        self.assertIsNone(assess(p)["requirements"][0]["passed"])

    def test_records_preserve_provenance_and_disposition(self):
        p = fixture()
        result = records(p)
        self.assertEqual(result, records(p))
        req = next(
            r
            for r in result
            if r["attributes"].get("requirement_id") == "evidence_supported"
        )
        self.assertEqual(req["attributes"]["langfuse_scores"], [p["scores"][0]])
        self.assertIn(
            "/traces/" + p["source_trace_id"], req["attributes"]["langfuse_trace_url"]
        )
        self.assertEqual(
            next(r for r in result if r["name"] == "application_outcome")["attributes"][
                "status"
            ],
            "approved_with_exceptions",
        )

    def test_returned_scores_do_not_change_assessment_identity(self):
        p = fixture()
        before = records(p)
        returned = copy.deepcopy(p["scores"][0])
        returned.update(id="witdem-returned", metadata={"source": "witdem"})
        p["scores"].append(returned)
        self.assertEqual(before, records(p))

    def test_derived_writeback_does_not_claim_overall_business_success(self):
        from witdem_langfuse.writeback import assessment_scores

        p = fixture()
        goal = next(r for r in records(p) if r["name"] == "product_goal")
        detail = {
            "summary": {
                "trace_id": goal["trace_id"],
                "execution_id": goal["execution_id"],
                "product_goal_reported": True,
            },
            "outcomes": {
                "product_goal": {**goal["attributes"], "witdem.source": "sdk"},
                "business": "approved_with_exceptions",
            },
        }
        values = assessment_scores(
            detail,
            project="p",
            trace_id=p["source_trace_id"],
            evidence_url="https://witdem.example/evidence",
        )
        self.assertIn("contract_requirements_met", [s["name"] for s in values])
        self.assertNotIn("business_outcome_achieved", [s["name"] for s in values])
        detail["outcomes"]["product_goal"]["product_goal_achieved"] = None
        values = assessment_scores(
            detail,
            project="p",
            trace_id=p["source_trace_id"],
            evidence_url="https://witdem.example/evidence",
        )
        self.assertNotIn("contract_requirements_met", [s["name"] for s in values])
        self.assertEqual(
            next(
                s["value"] for s in values if s["name"] == "contract_assessment_status"
            ),
            "unknown",
        )

    def test_snapshot_resume_and_offline(self):
        p = fixture()
        calls = []

        def fetch(cursor):
            calls.append(cursor)
            return {
                "data": [p["scores"][0 if cursor is None else 1]],
                "meta": {"cursor": "next" if cursor is None else None},
            }

        with tempfile.TemporaryDirectory() as tmp:
            snap = ScoreSnapshot(Path(tmp) / "scores.db", {"project": "p"}, fetch)
            self.assertFalse(snap.run(1)[0])
            self.assertTrue(snap.run(1)[0])
            self.assertEqual(len(snap.run(1)[1]), 2)
            self.assertEqual(calls, [None, "next"])

    def test_real_duckle_and_delivery_resume(self):
        p = fixture()
        self.assertEqual(duckle_assessment(p), records(p))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            manifest = path / "manifest.jsonl"
            manifest.write_text(json.dumps(p) + "\n")
            accepted = {}

            def send(body):
                r = json.loads(body)
                accepted[r["event_id"]] = r
                return {"event_id": r["event_id"], "status": "accepted"}

            delivery = ReplayDelivery(
                manifest,
                path / "delivery.db",
                "https://receiver",
                send=send,
                transform=duckle_assessment,
            )
            self.assertFalse(delivery.run(2)["complete"])
            self.assertTrue(delivery.run(100)["complete"])
            self.assertTrue(delivery.run(100)["complete"])
            self.assertEqual(len(accepted), len(records(p)))
