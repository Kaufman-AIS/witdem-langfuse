import copy
import unittest

from witdem_langfuse.writeback import assessment_scores

DETAIL = {
    "summary": {
        "trace_id": "trace",
        "execution_id": "execution",
        "product_goal_reported": True,
    },
    "outcomes": {
        "business": "manual_review_required",
        "product_goal": {
            "witdem.source": "sdk",
            "contract_hash": "hash",
            "contract_version": "3",
            "product_goal_achieved": True,
            "decision_evidence_sufficient": False,
        },
    },
}


class WritebackTest(unittest.TestCase):
    def scores(self, detail):
        return assessment_scores(
            detail,
            project="p",
            trace_id="trace",
            evidence_url="https://witdem.example/evidence",
        )

    def test_false_is_preserved_and_retry_ids_are_stable(self):
        scores = self.scores(DETAIL)
        self.assertEqual(
            [s["value"] for s in scores], [1.0, 0.0, "manual_review_required"]
        )
        self.assertEqual(scores, self.scores(DETAIL))
        self.assertTrue(all(s["metadata"]["contract_version"] == "3" for s in scores))

    def test_unknown_does_not_become_false(self):
        detail = copy.deepcopy(DETAIL)
        detail["outcomes"]["product_goal"]["decision_evidence_sufficient"] = None
        self.assertNotIn(
            "evidence_sufficient", [s["name"] for s in self.scores(detail)]
        )

    def test_observability_only_data_cannot_publish_business_scores(self):
        detail = copy.deepcopy(DETAIL)
        detail["summary"]["product_goal_reported"] = False
        with self.assertRaises(ValueError):
            self.scores(detail)
        detail = copy.deepcopy(DETAIL)
        detail["summary"]["trace_id"] = "other"
        with self.assertRaises(ValueError):
            self.scores(detail)
